"""The control loop end to end, with nftables, the clock and the neighbour
table faked out."""
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta
from pathlib import Path

from netflow.app import ApiError, Conf, Controller, CounterDeltas
from netflow.clients import Client
from netflow.model import Reason

TV = "1c:53:f9:16:66:68"
TV_IP = "192.168.50.101"
KB = 1024


class FakeNft:
    def __init__(self):
        self.bytes = {"acct_up": {}, "acct_down": {}, "acct_yt_up": {}, "acct_yt_down": {}}
        self.policy = None
        self.video = None  # pairs last pushed by the periodic resync

    def counters(self):
        return {s: dict(v) for s, v in self.bytes.items()}

    def apply_policy(self, p):
        self.policy = p

    def sync_video(self, pairs):
        self.video = sorted(pairs)

    def add(self, s, ip, n):
        self.bytes[s][ip] = self.bytes[s].get(ip, 0) + n


class Harness:
    def __init__(self, tmp: Path, now=datetime(2026, 7, 17, 12, 0), default_allow=True):
        self.now = now
        self.nft = FakeNft()
        self.clients = {TV: Client(mac=TV, ip=TV_IP, hostname="lv-chrome", present=True)}
        self.conf = Conf(state_path=str(tmp / "state.json"))
        self.ctl = self.make()
        self.ctl.st.cfg.default_allow = default_allow

    def make(self):
        return Controller(self.conf, self.nft, clock=lambda: self.now, snapshot=lambda: self.clients, uplink=lambda: True)

    def tick(self, n=1, up=0, down=0, yt=0):
        for _ in range(n):
            self.nft.add("acct_up", TV_IP, up)
            self.nft.add("acct_down", TV_IP, down)
            self.nft.add("acct_yt_down", TV_IP, yt)
            self.ctl.tick()
            self.now += timedelta(seconds=1)

    @property
    def dev(self):
        return self.ctl.st.devices[TV]


class ControlLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.h = Harness(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_registers_with_hostname_and_saves(self):
        self.h.tick()
        self.assertEqual(self.h.dev.name, "lv-chrome")
        self.assertIn(TV, self.h.nft.policy.known)
        saved = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(saved["devices"][0]["mac"], TV)

    def test_unapproved_newcomer_in_allowlist_mode(self):
        h = Harness(self.tmp / "b", default_allow=False)
        h.tick()
        self.assertFalse(h.dev.approved)
        self.assertEqual(h.ctl.rt[TV].reason, Reason.UNAPPROVED)
        self.assertIn(TV, h.nft.policy.blocked)
        self.assertFalse(h.nft.policy.default_allow)

    def test_usage_needs_traffic_over_threshold(self):
        self.h.tick()  # baseline reading
        self.h.tick(10, down=1 * KB)  # 10 KB/min: idle background
        self.assertEqual(self.h.dev.used_sec, 0)
        self.h.tick(10, down=50 * KB)  # streaming
        self.assertGreater(self.h.dev.used_sec, 0)
        self.assertEqual(self.h.dev.down_bytes, 10 * KB + 500 * KB)

    def test_quota_blocks_on_the_same_tick_and_checkpoints(self):
        self.h.tick()
        self.h.dev.quota_enabled, self.h.dev.quota_min = True, 1
        self.h.tick(70, down=50 * KB)
        self.assertEqual(self.h.dev.used_sec, 60)  # stops counting once cut
        self.assertEqual(self.h.ctl.rt[TV].reason, Reason.QUOTA)
        self.assertIn(TV, self.h.nft.policy.blocked)
        saved = json.loads((self.tmp / "state.json").read_text())
        self.assertEqual(saved["devices"][0]["used_sec"], 60)

    def test_yt_only_limit_cuts_youtube_only(self):
        self.h.tick()
        d = self.h.dev
        d.quota_enabled, d.quota_min, d.yt_only_limit = True, 1, True
        self.h.tick(70, down=50 * KB)  # the window needs a few seconds to fill
        self.assertEqual(self.h.ctl.rt[TV].reason, Reason.YT_QUOTA)
        self.assertNotIn(TV, self.h.nft.policy.blocked)
        self.assertIn(TV, self.h.nft.policy.ytblock)
        self.assertTrue(self.h.ctl.is_yt_blocked(TV_IP))
        self.assertFalse(self.h.ctl.is_yt_blocked("192.168.50.250"))

    def test_block_youtube_flag(self):
        self.h.tick()
        self.h.ctl.update_device({"mac": TV.upper(), "approved": True, "blockYoutube": True, "quotaMin": 480})
        self.assertIn(TV, self.h.nft.policy.ytblock)
        self.assertEqual(self.h.ctl.rt[TV].reason, Reason.ALLOWED)

    def test_youtube_time_counts_only_video_traffic(self):
        self.h.tick()
        self.h.tick(30, down=50 * KB)  # some other streaming app
        self.assertEqual(self.h.dev.yt_used_sec, 0)
        self.h.tick(30, down=50 * KB, yt=50 * KB)
        self.assertGreater(self.h.dev.yt_used_sec, 0)

    def test_daily_reset_and_extension_lapse(self):
        self.h.now = datetime(2026, 7, 17, 4, 50)
        self.h.tick()
        self.h.tick(5, down=50 * KB)
        self.h.ctl.extend({"mac": TV, "untilMin": 6 * 60})
        self.assertTrue(self.h.dev.used_sec > 0 and self.h.dev.extend_day)
        self.h.now = datetime(2026, 7, 17, 5, 0, 1)
        self.h.tick()
        # Still streaming across the reset: that very second belongs to the new day.
        self.assertLessEqual(self.h.dev.used_sec, 1)
        self.assertEqual((self.h.dev.extend_day, self.h.dev.down_bytes), (0, 0))
        self.assertEqual(self.h.ctl.st.day_key, 20260717)

    def test_state_survives_restart(self):
        self.h.tick()
        self.h.tick(5, down=50 * KB)
        self.h.ctl.update_device({"mac": TV, "name": "客廳電視", "approved": True, "quotaEnabled": True, "quotaMin": 90})
        used = self.h.dev.used_sec
        self.h.ctl.save()
        again = self.h.make()
        d = again.st.devices[TV]
        self.assertEqual((d.name, d.quota_enabled, d.quota_min, d.used_sec), ("客廳電視", True, 90, used))

    def test_offline_device_does_not_accrue(self):
        self.h.tick()
        self.h.clients[TV].present = False
        self.h.ctl.rt[TV].last_traffic = 0
        self.h.tick()
        self.assertFalse(self.h.ctl.rt[TV].online)

    def test_api_validation(self):
        self.h.tick()
        with self.assertRaises(ApiError) as e:
            self.h.ctl.update_device({"mac": "nonsense"})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            self.h.ctl.extend({"mac": "00:00:00:00:00:00", "untilMin": 60})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ApiError):
            self.h.ctl.extend({"mac": TV, "untilMin": 2000})
        self.h.ctl.update_global({"resetMin": 9999, "activeKBmin": 0, "defaultAllow": False, "blockEncDns": False})
        cfg = self.h.ctl.st.cfg
        self.assertEqual((cfg.reset_min, cfg.active_kbmin, cfg.default_allow, cfg.block_enc_dns), (1439, 1, False, False))
        self.assertFalse(self.h.nft.policy.block_enc_dns)

    def test_remove_device(self):
        self.h.tick()
        self.h.ctl.update_device({"mac": TV, "remove": True})
        self.assertNotIn(TV, self.h.ctl.st.devices)
        self.assertNotIn(TV, self.h.nft.policy.known)
        self.h.tick()  # still on the network: it comes back with defaults
        self.assertIn(TV, self.h.ctl.st.devices)

    def test_api_json_shape(self):
        self.h.tick()
        d = self.h.ctl.devices()["devices"][0]
        self.assertEqual(d["mac"], TV.upper())
        for k in ("ytUsedSec", "extendUntil", "extendActive", "blockYoutube", "ytOnlyLimit", "reason"):
            self.assertIn(k, d)
        s = self.h.ctl.status()
        for k in ("uplinkUp", "timeValid", "resetMin", "blockEncDns", "online"):
            self.assertIn(k, s)


class RecognitionHealth(unittest.TestCase):
    """The warning for YouTube video slipping past recognition."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))
        self.h.tick()

    def tearDown(self):
        self._tmp.cleanup()

    def run_for(self, secs, yt=0, app=True, down=200 * KB):
        for i in range(secs):
            if app and i % 10 == 0:
                self.h.ctl.note_query(TV_IP, "youtubei.googleapis.com")
            self.h.tick(down=down, yt=yt)

    @property
    def warn(self):
        return self.h.ctl.rt[TV].yt_warn

    def test_warns_when_app_streams_unrecognised(self):
        self.run_for(200)
        self.assertFalse(self.warn)  # tell holds, but not yet for long enough
        self.run_for(60)
        self.assertTrue(self.warn)
        self.assertTrue(self.h.ctl.devices()["devices"][0]["ytDetectWarn"])
        self.assertEqual(self.h.ctl.status()["ytDetectWarn"], 1)

    def test_quiet_when_video_is_recognised(self):
        self.run_for(300, yt=200 * KB)
        self.assertFalse(self.warn)

    def test_quiet_without_the_app(self):
        self.run_for(300, app=False)  # some other streaming app
        self.assertFalse(self.warn)

    def test_quiet_under_a_youtube_block(self):
        self.h.dev.block_youtube = True
        self.run_for(300)
        self.assertFalse(self.warn)

    def test_quiet_on_light_traffic(self):
        self.run_for(300, down=10 * KB)  # browsing the app, not watching
        self.assertFalse(self.warn)

    def test_clears_after_recovery(self):
        self.run_for(260)
        self.assertTrue(self.warn)
        self.run_for(200, yt=200 * KB)
        self.assertTrue(self.warn)  # held a while, no flapping
        self.run_for(400, yt=200 * KB)
        self.assertFalse(self.warn)

    def test_tv_app_lookups_count(self):
        # The TV app loads www.youtube.com rather than youtubei.googleapis.com.
        self.h.ctl.note_query(TV_IP, "www.youtube.com")
        self.h.ctl.note_query(TV_IP, "m.youtube.com")
        self.assertEqual(len(self.h.ctl.rt[TV].app_lookups), 2)

    def test_thresholds_from_conf(self):
        self.run_for(60)
        self.assertFalse(self.warn)  # defaults: 20 MB in, then 120 s more
        self.h.conf.health_min_mb, self.h.conf.health_raise_sec = 5, 30
        self.run_for(25)
        self.assertFalse(self.warn)
        self.run_for(10)
        self.assertTrue(self.warn)

    def test_window_from_conf(self):
        self._tmp2 = tempfile.TemporaryDirectory()
        h = Harness(Path(self._tmp2.name))
        h.conf.health_window_sec = 30
        h.ctl = h.make()
        h.tick()
        self.assertEqual(len(h.ctl.rt[TV].long_all._win), 30)
        self._tmp2.cleanup()

    def test_query_log(self):
        # (assertNoLogs is 3.10+; the tests also run on 3.9)
        with mock.patch("netflow.app.log") as log:
            self.h.ctl.note_query(TV_IP, "www.example.com")
        log.info.assert_not_called()
        self.h.conf.log_queries = True
        with self.assertLogs("netflow.app", level="INFO") as cm:
            self.h.ctl.note_query(TV_IP, "www.example.com")
        self.assertIn(f"query {TV_IP} www.example.com", cm.output[0])

    def test_other_lookups_ignored(self):
        for _ in range(5):
            self.h.ctl.note_query(TV_IP, "www.google.com")
            self.h.ctl.note_query("192.168.50.250", "youtubei.googleapis.com")  # unknown client
        self.assertEqual(self.h.ctl.rt[TV].app_lookups, [])


class ConfLoad(unittest.TestCase):
    def test_domains_normalised_and_unknown_keys_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "netflow.json"
            p.write_text(json.dumps({
                "video_domains": ["GoogleVideo.com.", " newcdn.net ", ""],
                "video_timeout_s": 21600,  # dropped option: ignored, not an error
                "health_min_mb": 50,
            }))
            c = Conf.load(p)
        self.assertEqual(c.video_domains, ["googlevideo.com", "newcdn.net"])
        self.assertEqual(c.health_min_mb, 50)
        self.assertIn("dns.google", c.enc_dns_domains)
        self.assertFalse(c.log_queries)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(Conf.load(Path("/nonexistent/netflow.json")), Conf())

    def test_enc_dns_flag_follows_setting(self):
        with tempfile.TemporaryDirectory() as d:
            h = Harness(Path(d))
            self.assertTrue(h.ctl.enc_dns_blocked())
            h.ctl.st.cfg.block_enc_dns = False
            self.assertFalse(h.ctl.enc_dns_blocked())


class Reminders(unittest.TestCase):
    """Warn before a time limit cuts, and when it has -- once per run-up."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name), now=datetime(2026, 7, 17, 20, 0))
        self.h.tick()
        d = self.h.dev
        d.name, d.notify_enabled, d.notify_warn_min = "客廳電視", True, 10
        d.quota_enabled, d.quota_min = True, 15
        self.h.ctl.drain_outbox()

    def tearDown(self):
        self._tmp.cleanup()

    def run_for(self, secs, down=50 * KB):
        out = []
        for _ in range(secs):
            self.h.tick(down=down)
            out += self.h.ctl.drain_outbox()
        return [n.big for n in out], out

    def test_warn_cut_extend_warn_cut(self):
        # 15 min quota, warn at 10 min left: the warning comes after ~5 min of use.
        bigs, out = self.run_for(15 * 60 + 10)
        self.assertEqual(bigs, ["還剩 10 分鐘", "時間到了"])
        self.assertEqual(out[0].ip, TV_IP)
        self.assertIn("今天的上網時數已用完", out[1].sub)
        # Extended by 15 minutes: allowed again, warned again, cut again.
        until = (self.h.now + timedelta(minutes=15)).hour * 60 + (self.h.now + timedelta(minutes=15)).minute
        self.h.ctl.extend({"mac": TV, "untilMin": until})
        bigs, out = self.run_for(16 * 60)
        self.assertEqual(bigs, ["還剩 10 分鐘", "時間到了"])
        self.assertIn("延長時間到", out[0].sub)

    def test_short_extension_still_warns(self):
        self.run_for(15 * 60 + 10)
        until = (self.h.now + timedelta(minutes=4)).hour * 60 + (self.h.now + timedelta(minutes=4)).minute
        self.h.ctl.extend({"mac": TV, "untilMin": until})
        bigs, _ = self.run_for(5 * 60)
        self.assertEqual(len(bigs), 2)
        self.assertTrue(bigs[0].startswith("還剩 ") and bigs[0] != "還剩 10 分鐘")  # 3 or 4 minutes
        self.assertEqual(bigs[1], "時間到了")

    def test_raised_quota_rearms(self):
        self.run_for(15 * 60 + 10)
        self.h.dev.quota_min = 30
        bigs, _ = self.run_for(15 * 60 + 10)
        self.assertEqual(bigs, ["還剩 10 分鐘", "時間到了"])

    def test_restart_does_not_replay(self):
        self.run_for(6 * 60)  # inside the warning period, already warned
        self.h.ctl.save()
        self.h.ctl = self.h.make()
        bigs, _ = self.run_for(60)
        self.assertEqual(bigs, [])

    def test_window_and_yt_only_wording(self):
        d = self.h.dev
        d.quota_enabled, d.yt_only_limit = False, True
        d.win_enabled, d.win_start, d.win_end = True, 6 * 60, 20 * 60 + 15
        bigs, out = self.run_for(16 * 60)
        self.assertEqual(bigs, ["YouTube 還剩 10 分鐘", "YouTube 時間到了"])
        self.assertIn("可用時段到 20:15", out[0].sub)
        self.assertIn("其他 App 可以繼續使用", out[1].sub)

    def test_disabled_sends_nothing_and_keeps_no_backlog(self):
        self.h.dev.notify_enabled = False
        self.assertEqual(self.run_for(15 * 60 + 10)[0], [])
        self.h.dev.notify_enabled = True
        self.assertEqual(self.run_for(30)[0], [])  # no stale reminders on enabling

    def test_idle_device_does_not_count_down(self):
        bigs, _ = self.run_for(15 * 60, down=0)
        self.assertEqual(bigs, [])  # quota runs on use only

    def test_api(self):
        dev = self.h.ctl.devices()["devices"][0]
        self.assertEqual((dev["notifyEnabled"], dev["notifyWarnMin"]), (True, 10))
        self.h.ctl.update_device({"mac": TV, "approved": True, "quotaMin": 480, "notifyEnabled": True, "notifyWarnMin": 999})
        self.assertEqual(self.h.dev.notify_warn_min, 120)
        self.h.ctl.notify_test({"mac": TV})
        self.assertEqual(self.h.ctl.drain_outbox()[0].big, "通知測試")
        self.h.clients[TV].ip = ""
        self.h.tick()
        with self.assertRaises(ApiError) as e:
            self.h.ctl.notify_test({"mac": TV})
        self.assertEqual(e.exception.status, 409)


class VideoPairs(unittest.TestCase):
    """Learned video addresses survive a restart and a ruleset reload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))
        self.h.tick()

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_new_pairs_go_in_at_once(self):
        ctl = self.h.ctl
        self.assertEqual(ctl.learn_video(TV_IP, [("1.1.1.1", 300), ("2.2.2.2", 300)]), [(TV_IP, "1.1.1.1"), (TV_IP, "2.2.2.2")])
        self.assertEqual(ctl.learn_video(TV_IP, [("1.1.1.1", 300)]), [])  # known: resync keeps it
        self.assertEqual(ctl.learn_video("192.168.50.150", [("1.1.1.1", 300)]), [("192.168.50.150", "1.1.1.1")])

    def test_restored_after_restart(self):
        self.h.ctl.learn_video(TV_IP, [("210.242.128.143", 300)])
        self.h.ctl.save()
        self.h.ctl = self.h.make()  # the daemon restarts; nftables has lost the set
        self.h.tick()  # the first tick re-inserts the saved pairs
        self.assertEqual(self.h.nft.video, [(TV_IP, "210.242.128.143")])

    def test_resynced_periodically(self):
        self.h.ctl.learn_video(TV_IP, [("1.1.1.1", 300)])
        self.h.ctl._last_video_sync -= self.h.conf.video_resync_sec  # a minute has gone by
        self.h.nft.video = None
        self.h.tick()
        self.assertEqual(self.h.nft.video, [(TV_IP, "1.1.1.1")])

    def test_forgotten_after_the_keep_window(self):
        ctl = self.h.ctl
        ctl.learn_video(TV_IP, [("1.1.1.1", 300), ("2.2.2.2", 300)])
        ctl.st.video_pairs[(TV_IP, "1.1.1.1")] -= self.h.conf.video_keep_sec + 1
        ctl._resync_video()
        self.assertEqual(self.h.nft.video, [(TV_IP, "2.2.2.2")])
        self.assertNotIn((TV_IP, "1.1.1.1"), ctl.st.video_pairs)


class NowPlaying(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))
        self.h.tick()

    def tearDown(self):
        self._tmp.cleanup()

    def test_report_shows_the_playing_session(self):
        body = {"sessions": [
            {"package": "com.spotify.tv.android", "state": "paused", "title": "EP198", "artist": "老師不正經"},
            {"package": "com.google.android.youtube.tv", "state": "playing", "title": "影片", "artist": "某頻道", "positionMs": 1234},
        ]}
        with self.assertLogs("netflow.app", "INFO") as cm:
            self.h.ctl.now_playing(TV_IP, body)
        self.assertIn("影片 — 某頻道", cm.output[0])
        d = self.h.ctl.devices()["devices"][0]
        self.assertEqual(d["nowPlaying"]["artist"], "某頻道")
        self.assertEqual(d["nowPlaying"]["positionMs"], 1234)

    def test_nothing_playing(self):
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": "x", "state": "paused", "title": "t"}]})
        self.assertIsNone(self.h.ctl.devices()["devices"][0]["nowPlaying"])

    def test_stale_report_hidden(self):
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": "x", "state": "playing", "title": "t"}]})
        self.h.ctl.rt[TV].now_playing_at -= 600
        self.assertIsNone(self.h.ctl.devices()["devices"][0]["nowPlaying"])

    def test_rejects_unknown_client_and_bad_body(self):
        with self.assertRaises(ApiError) as e:
            self.h.ctl.now_playing("192.168.50.250", {"sessions": []})
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(ApiError):
            self.h.ctl.now_playing(TV_IP, {"sessions": "nope"})
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": "x" * 999, "state": "playing", "positionMs": "bad"}, 5]})
        np = self.h.ctl.devices()["devices"][0]["nowPlaying"]
        self.assertEqual(len(np["package"]), 200)
        self.assertNotIn("positionMs", np)


class WatchHistory(unittest.TestCase):
    YT = "com.google.android.youtube.tv"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))
        self.h.tick()

    def tearDown(self):
        self._tmp.cleanup()

    def play(self, title, channel, state="playing", pkg=None):
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": pkg or self.YT, "state": state, "title": title, "artist": channel}]})

    def hist(self, days=1, mac=""):
        return self.h.ctl.watch_history(days, mac)["devices"]

    def test_counts_playing_seconds_by_channel_and_title(self):
        self.play("影片A", "台南Josh")
        self.h.tick(30)
        self.play("影片B", "台南Josh")
        self.h.tick(20)
        self.play("Song", "某歌手", pkg="com.spotify.tv.android")
        self.h.tick(10)
        dev = self.hist()[0]
        self.assertEqual(dev["seconds"], 60)
        josh, song = dev["channels"]
        self.assertEqual((josh["channel"], josh["app"], josh["seconds"]), ("台南Josh", "YouTube", 50))
        self.assertEqual([t["title"] for t in josh["titles"]], ["影片A", "影片B"])
        self.assertEqual((song["app"], song["seconds"]), ("Spotify", 10))

    def test_untitled_still_counts(self):
        # The YouTube TV app reports Shorts as playing with no title or channel.
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": self.YT, "state": "playing"}]})
        self.h.tick(20)
        self.h.ctl.now_playing(TV_IP, {"sessions": [{"package": "hami.androidtv", "state": "playing"}]})
        self.h.tick(5)
        chans = {c["channel"]: c for c in self.hist()[0]["channels"]}
        self.assertEqual(chans["Shorts／無標題"]["seconds"], 20)
        self.assertEqual(chans["Shorts／無標題"]["app"], "YouTube")
        self.assertEqual(chans["Shorts／無標題"]["titles"][0]["title"], "（無標題）")
        self.assertEqual(chans["（無標題）"]["app"], "Hami Video")

    def test_paused_and_stale_do_not_count(self):
        self.play("影片A", "ch", state="paused")
        self.h.tick(30)
        self.assertEqual(self.hist(), [])
        self.play("影片A", "ch")
        self.h.ctl.rt[TV].now_playing_at -= 120  # the app stopped reporting (TV off)
        self.h.tick(30)
        self.assertEqual(self.hist(), [])

    def test_days_and_device_filter_and_retention(self):
        hist = self.h.ctl.history
        day = self.h.ctl.st.day_key
        hist.add(20260101, TV, self.YT, "old", "t", 100, 0)  # far past the 90 days
        hist.add(day - 1 if day % 100 > 1 else day, TV, self.YT, "yesterday", "t", 50, 0)
        hist.add(day, "aa:bb:cc:dd:ee:ff", self.YT, "other device", "t", 5, 0)
        self.assertEqual(sum(d["seconds"] for d in self.hist(days=1)), 5 + (50 if day % 100 == 1 else 0))
        self.assertEqual(self.hist(days=1, mac="AA:BB:CC:DD:EE:FF")[0]["seconds"], 5)
        self.assertNotIn("old", [c["channel"] for d in self.hist(days=90) for c in d["channels"]])
        self.h.ctl.save()
        self.h.ctl = self.h.make()  # prunes on start
        self.assertEqual([r for r in hist.query(0) if r["channel"] == "old"], [])

    def test_survives_restart(self):
        self.play("影片A", "台南Josh")
        self.h.tick(15)
        self.h.ctl.save()
        self.h.ctl = self.h.make()
        self.assertEqual(self.hist()[0]["seconds"], 15)


class BlockShorts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))
        self.h.tick()

    def tearDown(self):
        self._tmp.cleanup()

    def report(self, **extra):
        return self.h.ctl.now_playing(TV_IP, dict({"sessions": []}, **extra))

    def test_policy_in_the_reply(self):
        self.assertEqual(self.report(), {"blockShorts": False})
        self.h.ctl.update_device({"mac": TV, "approved": True, "quotaMin": 480, "blockShorts": True})
        self.assertEqual(self.report(), {"blockShorts": True})
        self.assertTrue(self.h.ctl.devices()["devices"][0]["blockShorts"])

    def test_notice_when_the_app_blocked_one_at_most_once_a_minute(self):
        self.h.ctl.drain_outbox()
        self.report(blockedShorts=True)
        self.report(blockedShorts=True)
        out = self.h.ctl.drain_outbox()
        self.assertEqual([n.big for n in out], ["Shorts 已封鎖"])
        self.assertEqual(out[0].ip, TV_IP)
        self.h.ctl.rt[TV].shorts_notice_at -= 61
        self.report(blockedShorts=True)
        self.assertEqual(len(self.h.ctl.drain_outbox()), 1)

    def test_setting_survives_restart(self):
        self.h.ctl.update_device({"mac": TV, "approved": True, "quotaMin": 480, "blockShorts": True})
        self.h.ctl = self.h.make()
        self.assertTrue(self.h.ctl.st.devices[TV].block_shorts)


class Deltas(unittest.TestCase):
    def test_baseline_new_and_recreated(self):
        c = CounterDeltas()
        self.assertEqual(c.update({"acct_up": {"a": 1000}}), {"acct_up": {}})  # baseline only
        self.assertEqual(c.update({"acct_up": {"a": 1500, "b": 300}}), {"acct_up": {"a": 500, "b": 300}})
        self.assertEqual(c.update({"acct_up": {"a": 100, "b": 300}}), {"acct_up": {"a": 100, "b": 0}})


if __name__ == "__main__":
    unittest.main()
