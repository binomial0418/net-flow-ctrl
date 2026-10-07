"""The once-a-second control loop and the API it serves. Mirrors the tick in
esp32/esp32.ino: register clients, roll the day, collect bytes, accrue usage,
evaluate rules and push the verdicts to nftables."""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clients, dnsmsg, rules, store
from .history import History, day_minus, summarise
from .model import FULL_BLOCK, TIME_CUT, YT_LIMIT, DeviceRule, Reason
from .nft import Policy
from .notifier import Notice

log = logging.getLogger(__name__)

USAGE_SAVE_SEC = 60  # periodic checkpoint of the usage counters
MAX_VIDEO_PAIRS = 4096  # learned (client, video address) pairs kept at most
NOW_PLAYING_STALE_SEC = 180  # the app reports every minute; older means it is gone
# Viewing history counts a second only while the last report is this fresh: the
# app reports every minute, so a TV switched off mid-video stops counting soon.
WATCH_FRESH_SEC = 90
ONLINE_GRACE_SEC = 120  # recent traffic keeps a device "online" past its neighbour entry

# YouTube recognition health. Recognition hangs on seeing the DNS lookups; a
# client that finds a way around them (a new encrypted-DNS endpoint, say) would
# silently escape both timing and blocking. The tell: the YouTube app is in
# use and plenty of data is moving, yet almost none of it is recognised video.
# Judged over a trailing health_window_sec, raised only once that has held for
# health_raise_sec, and cleared after health_clear_sec without it, so a brief
# overlap (the app opened, then another streaming app) does not flash a warning.
# The thresholds live in Conf so they can be tuned from netflow.json.

# Conf fields holding domain lists, normalised on load.
DOMAIN_LISTS = ("youtube_domains", "video_domains", "youtube_app_domains", "enc_dns_domains")


@dataclass
class Conf:
    lan_if: str = "ens19"
    wan_if: str = "ens18"
    lan_ip: str = "192.168.50.1"
    http_port: int = 80
    upstream_dns: List[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1", "168.95.192.1"])
    state_path: str = "/var/lib/netflow/state.json"
    leases_path: str = "/var/lib/misc/dnsmasq.leases"
    max_devices: int = 64
    # Viewing history (history.py); empty path: history.db next to the state file.
    history_path: str = ""
    history_keep_days: int = 90
    # Should YouTube move, these can be overridden without touching the code.
    youtube_domains: List[str] = field(default_factory=lambda: list(dnsmsg.YOUTUBE_DOMAINS))
    video_domains: List[str] = field(default_factory=lambda: list(dnsmsg.VIDEO_DOMAINS))
    youtube_app_domains: List[str] = field(default_factory=lambda: list(dnsmsg.YOUTUBE_APP_DOMAINS))
    # Answered NXDOMAIN while the "block encrypted DNS" setting is on.
    enc_dns_domains: List[str] = field(default_factory=lambda: list(dnsmsg.ENC_DNS_DOMAINS))
    # Log every lookup (client, name): for checking which names a device's
    # YouTube app really uses. Noisy; leave off in normal use.
    log_queries: bool = False
    # Recognition health (see the comment above).
    health_window_sec: int = 300
    health_min_mb: int = 20
    health_min_app_lookups: int = 2
    health_max_yt_share: float = 0.05
    health_raise_sec: int = 120
    health_clear_sec: int = 300
    # Learned video addresses are kept this long after their last lookup and
    # re-inserted into nftables every video_resync_sec meanwhile.
    video_keep_sec: int = 24 * 3600
    video_resync_sec: int = 60
    # Reminders through TvOverlay on the devices (notifier.py).
    tvoverlay_port: int = 5001
    notify_duration_s: int = 20

    @classmethod
    def load(cls, path: Path) -> "Conf":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        names = {f.name for f in fields(cls)}
        conf = cls(**{k: v for k, v in data.items() if k in names})
        for k in DOMAIN_LISTS:
            # "YouTube.com." in the file still matches youtube.com.
            setattr(conf, k, [d for d in map(dnsmsg.norm_domain, getattr(conf, k)) if d])
        return conf


class ApiError(Exception):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(text)
        self.status = status
        self.text = text


@dataclass
class Runtime:
    ip: str = ""
    present: bool = False
    online: bool = False
    last_traffic: float = 0.0
    reason: Reason = Reason.ALLOWED
    act: rules.ActivityWindow = field(default_factory=rules.ActivityWindow)
    yt_act: rules.ActivityWindow = field(default_factory=rules.ActivityWindow)
    # Recognition health (see Conf.health_*), over Conf.health_window_sec.
    long_all: rules.ActivityWindow = field(default_factory=rules.ActivityWindow)
    long_yt: rules.ActivityWindow = field(default_factory=rules.ActivityWindow)
    app_lookups: List[float] = field(default_factory=list)  # monotonic times
    suspect_sec: int = 0  # consecutive seconds the tell has held
    clear_sec: int = 0  # consecutive seconds it has not
    yt_warn: bool = False
    # What the device reports it is playing (the NetFlow TV app, tvapp/).
    now_playing: List[Dict[str, Any]] = field(default_factory=list)
    now_playing_at: float = 0.0  # wall clock of the last report
    # Reminders (see _check_notices). Not persisted: the first tick after a
    # start only primes them, so a restart never replays a reminder.
    notice_primed: bool = False
    warn_armed: bool = False
    cut_armed: bool = False


class CounterDeltas:
    """Turns nftables' free-running per-IP byte counters into per-tick deltas.
    The first reading is only a baseline (bytes from before this process
    started were already counted); an element that appears later counts in
    full, and one that went backwards was re-created and starts over."""

    def __init__(self) -> None:
        self._prev: Optional[Dict[Tuple[str, str], int]] = None

    def update(self, cur: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
        first = self._prev is None
        prev = self._prev or {}
        out: Dict[str, Dict[str, int]] = {}
        nxt: Dict[Tuple[str, str], int] = {}
        for s, per_ip in cur.items():
            out[s] = {}
            for ip, b in per_ip.items():
                nxt[(s, ip)] = b
                p = prev.get((s, ip))
                if first:
                    continue
                out[s][ip] = b - p if p is not None and b >= p else b
        self._prev = nxt
        return out


def norm_mac(s: Any) -> str:
    parts = str(s).strip().lower().split(":")
    if len(parts) != 6 or not all(len(p) == 2 and all(c in "0123456789abcdef" for c in p) for p in parts):
        raise ApiError(400, "bad mac")
    return ":".join(parts)


def clamp_min(v: Any) -> int:
    return max(0, min(1439, int(v)))


def uplink_up(wan_if: str) -> bool:
    """A default route out of the WAN interface."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            return any(ln.split()[0] == wan_if and ln.split()[1] == "00000000" for ln in f)
    except OSError:
        return False


class Controller:
    def __init__(
        self,
        conf: Conf,
        nft,
        clock: Callable[[], datetime] = datetime.now,
        snapshot: Optional[Callable[[], Dict[str, clients.Client]]] = None,
        uplink: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.conf = conf
        self.nft = nft
        self.clock = clock
        self._snapshot = snapshot or (lambda: clients.snapshot(conf.lan_if, Path(conf.leases_path)))
        self._uplink = uplink or (lambda: uplink_up(conf.wan_if))
        self.st = store.load(Path(conf.state_path))
        self.rt: Dict[str, Runtime] = {mac: self._new_rt() for mac in self.st.devices}
        self.ip_to_mac: Dict[str, str] = {}
        self.deltas = CounterDeltas()
        self.uplink_up = False
        self.time_valid = False
        self.started = time.monotonic()
        self._last_save = time.monotonic()
        self._last_video_sync = 0.0  # 0: restore the saved pairs on the first tick
        self.outbox: List[Notice] = []  # reminders for the main loop to deliver
        self.history = History(Path(conf.history_path or Path(conf.state_path).with_name("history.db")))
        if self.st.day_key:
            self.history.prune(day_minus(self.st.day_key, conf.history_keep_days - 1))

    # ------------------------------------------------------------- helpers --

    def _now(self) -> Tuple[datetime, int]:
        now = self.clock()
        return now, now.hour * 60 + now.minute

    def _new_rt(self) -> Runtime:
        w = max(1, self.conf.health_window_sec)
        return Runtime(long_all=rules.ActivityWindow(w), long_yt=rules.ActivityWindow(w))

    def save(self) -> None:
        store.save(Path(self.conf.state_path), self.st)
        self.history.flush()
        self._last_save = time.monotonic()

    def is_yt_blocked(self, client_ip: str) -> bool:
        mac = self.ip_to_mac.get(client_ip)
        d = self.st.devices.get(mac) if mac else None
        if d is None:
            return False
        return d.block_youtube or self.rt[mac].reason in YT_LIMIT

    def enc_dns_blocked(self) -> bool:
        return self.st.cfg.block_enc_dns

    def note_query(self, client_ip: str, name: str) -> None:
        """Called by the DNS proxy for every lookup."""
        if self.conf.log_queries:
            log.info("query %s %s", client_ip, name)
        if not dnsmsg.matches(name, self.conf.youtube_app_domains):
            return
        mac = self.ip_to_mac.get(client_ip)
        if mac in self.rt:
            self.rt[mac].app_lookups.append(time.monotonic())

    # ---------------------------------------------------------------- tick --

    def tick(self) -> None:
        now, _ = self._now()
        self.time_valid = now.year >= 2020
        self.uplink_up = self._uplink()
        self._refresh_clients()
        self._check_daily_reset(now)
        self._collect_and_accrue()
        self._accrue_watch()
        self.sync()  # after accruing, so a device that just ran out is cut this tick
        self._check_notices(now)
        if time.monotonic() - self._last_video_sync >= self.conf.video_resync_sec or not self._last_video_sync:
            self._resync_video()
        if time.monotonic() - self._last_save >= USAGE_SAVE_SEC:
            self.save()

    def _refresh_clients(self) -> None:
        snap = self._snapshot()
        self.ip_to_mac = {c.ip: mac for mac, c in snap.items() if c.ip}
        registered = False
        for mac, c in snap.items():
            if mac not in self.st.devices:
                if len(self.st.devices) >= self.conf.max_devices:
                    log.warning("device table full, ignoring %s", mac)
                    continue
                # A newcomer inherits the global policy: in allowlist mode it
                # lands here unapproved and waits for the admin.
                self.st.devices[mac] = DeviceRule(
                    mac=mac, name=clients.default_name(c, mac), approved=self.st.cfg.default_allow
                )
                self.rt[mac] = self._new_rt()
                registered = True
                log.info("registered %s (%s)", mac, c.hostname or "-")
        session_ended = False
        mono = time.monotonic()
        for mac in self.st.devices:
            rt = self.rt[mac]
            c = snap.get(mac)
            rt.ip = c.ip if c else ""
            rt.present = bool(c and c.present)
            online = rt.present or (mono - rt.last_traffic < ONLINE_GRACE_SEC and rt.last_traffic > 0)
            if rt.online and not online:
                session_ended = True
            rt.online = online
        if registered or session_ended:
            # A device leaving is a natural checkpoint for what it used.
            self.save()

    def _check_daily_reset(self, now: datetime) -> None:
        if not self.time_valid:
            return
        key = rules.day_key(now, self.st.cfg.reset_min)
        if self.st.day_key == 0:
            self.st.day_key = key  # first run with a valid clock: adopt, do not wipe
            self.save()
        elif key != self.st.day_key:
            self.st.day_key = key
            self.history.prune(day_minus(key, self.conf.history_keep_days - 1))
            self.reset_usage()

    def _accrue_watch(self) -> None:
        """One second of viewing for every device whose last report, still
        fresh, has something playing. Paused does not count."""
        if not (self.time_valid and self.st.day_key):
            return
        now = time.time()
        for mac, rt in self.rt.items():
            if now - rt.now_playing_at >= WATCH_FRESH_SEC:
                continue
            p = _playing(rt.now_playing)
            if p and p.get("state") == "playing" and p.get("title"):
                self.history.add(self.st.day_key, mac, p.get("package", ""), p.get("artist") or "",
                                 p["title"], 1, now)

    def watch_history(self, days: int = 1, mac: str = "") -> Dict[str, Any]:
        """Viewing over the last `days` logical days (1 = today), per device
        and channel, optionally for one device."""
        days = max(1, min(self.conf.history_keep_days, int(days)))
        today = self.st.day_key or rules.day_key(self.clock(), self.st.cfg.reset_min)
        since = day_minus(today, days - 1)
        rows = self.history.query(since, norm_mac(mac) if mac else None)
        names = {m: d.name for m, d in self.st.devices.items()}
        return {"since": since, "until": today, "devices": summarise(rows, names)}

    def _collect_and_accrue(self) -> None:
        d = self.deltas.update(self.nft.counters())
        per_mac: Dict[str, List[int]] = {}
        for s, idx in (("acct_up", 0), ("acct_down", 1), ("acct_yt_up", 2), ("acct_yt_down", 3)):
            for ip, b in d.get(s, {}).items():
                mac = self.ip_to_mac.get(ip)
                if mac in self.st.devices:
                    per_mac.setdefault(mac, [0, 0, 0, 0])[idx] += b
        threshold = self.st.cfg.active_kbmin * 1024
        mono = time.monotonic()
        for mac, dev in self.st.devices.items():
            rt = self.rt[mac]
            up, down, yt_up, yt_down = per_mac.get(mac, (0, 0, 0, 0))
            dev.up_bytes += up
            dev.down_bytes += down
            if up or down:
                rt.last_traffic = mono
            # The windows roll for every device (0 when idle) so they stay honest.
            if rt.act.tick(up + down, threshold) and rt.online and rt.reason == Reason.ALLOWED:
                dev.used_sec += 1
            # YouTube time is informational and judged on its own share of the
            # traffic, whatever the verdict: a YouTube cut leaves nothing to count.
            if rt.yt_act.tick(yt_up + yt_down, threshold) and rt.online:
                dev.yt_used_sec += 1
            self._check_health(mac, dev, rt, up + down, yt_up + yt_down, mono)

    def _check_health(self, mac: str, dev: DeviceRule, rt: Runtime, total: int, yt: int, mono: float) -> None:
        rt.long_all.tick(total)
        rt.long_yt.tick(yt)
        c = self.conf
        rt.app_lookups = [t for t in rt.app_lookups if mono - t < c.health_window_sec]
        # Under a YouTube cut there is meant to be no video traffic.
        tell = (
            not (dev.block_youtube or rt.reason in YT_LIMIT)
            and len(rt.app_lookups) >= c.health_min_app_lookups
            and rt.long_all.total >= c.health_min_mb * 1024 * 1024
            and rt.long_yt.total < rt.long_all.total * c.health_max_yt_share
        )
        if tell:
            rt.suspect_sec += 1
            rt.clear_sec = 0
        else:
            rt.clear_sec += 1
            rt.suspect_sec = 0
        if not rt.yt_warn and rt.suspect_sec >= c.health_raise_sec:
            rt.yt_warn = True
            log.warning(
                "%s (%s): YouTube app in use but its video is not being recognised -- "
                "DNS may be bypassing the proxy", dev.name, mac
            )
        elif rt.yt_warn and rt.clear_sec >= c.health_clear_sec:
            rt.yt_warn = False
            log.info("%s (%s): YouTube recognition back to normal", dev.name, mac)

    def sync(self) -> None:
        _, now_min = self._now()
        quota_hit = False
        known, blocked, ytblock = set(), set(), set()
        for mac, dev in self.st.devices.items():
            rt = self.rt[mac]
            r = rules.evaluate(
                dev,
                now_min,
                uplink_up=self.uplink_up,
                time_valid=self.time_valid,
                today=self.st.day_key,
                reset_min=self.st.cfg.reset_min,
            )
            if r in (Reason.QUOTA, Reason.YT_QUOTA) and rt.reason != r:
                quota_hit = True
            rt.reason = r
            known.add(mac)
            if r in FULL_BLOCK:
                blocked.add(mac)
            if dev.block_youtube or r in YT_LIMIT:
                ytblock.add(mac)
        self.nft.apply_policy(
            Policy(
                known=frozenset(known),
                blocked=frozenset(blocked),
                ytblock=frozenset(ytblock),
                default_allow=self.st.cfg.default_allow,
                block_enc_dns=self.st.cfg.block_enc_dns,
            )
        )
        # Exhausting a quota is the moment where losing the last minutes of
        # counting would hand back an allowance, so checkpoint it at once.
        if quota_hit:
            self.save()

    # ------------------------------------------------------- video addresses --

    def learn_video(self, client_ip: str, addrs: List[Tuple[str, int]]) -> List[Tuple[str, str]]:
        """Record video addresses a client just looked up; returns the pairs
        that are new and must go into nftables before the client sees the
        answer. Known pairs only have their lookup time refreshed: the
        periodic resync keeps them in the ruleset."""
        now = time.time()
        fresh = []
        for ip, _ttl in addrs:
            k = (client_ip, ip)
            if k not in self.st.video_pairs:
                fresh.append(k)
            self.st.video_pairs[k] = now
        return fresh

    def _resync_video(self) -> None:
        """Re-insert every pair looked up within video_keep_sec. A reload of
        nftables.service or a restart empties the kernel set, and the YouTube
        app keeps streaming from addresses it resolved before -- without this
        that video would go unrecognised (no timing, no YouTube cut)."""
        self._last_video_sync = time.monotonic()
        cutoff = time.time() - self.conf.video_keep_sec
        pairs = self.st.video_pairs
        for k in [k for k, t in pairs.items() if t < cutoff]:
            del pairs[k]
        if len(pairs) > MAX_VIDEO_PAIRS:  # keep the most recently looked up
            for k, _ in sorted(pairs.items(), key=lambda kv: kv[1])[: len(pairs) - MAX_VIDEO_PAIRS]:
                del pairs[k]
        if pairs:
            self.nft.sync_video(list(pairs))

    # ----------------------------------------------------------- reminders --

    def _check_notices(self, now: datetime) -> None:
        """Warn before a time limit cuts, and say so when it has. Not once a
        day but once per run-up to a cut: when time is added back (an
        extension, a raised quota) both reminders re-arm, so a device that is
        extended and runs out again is told again."""
        for mac, dev in self.st.devices.items():
            rt = self.rt[mac]
            left = rules.time_left(
                dev, now, time_valid=self.time_valid, today=self.st.day_key, reset_min=self.st.cfg.reset_min
            )
            allowed = rt.reason == Reason.ALLOWED
            cut = rt.reason in TIME_CUT
            warn_s = dev.notify_warn_min * 60
            due = allowed and left is not None and 0 < left[0] <= warn_s
            if not rt.notice_primed:
                rt.warn_armed, rt.cut_armed, rt.notice_primed = not due, not cut, True
                continue
            if allowed:
                if not rt.cut_armed:
                    # Back from a cut: a new run-up, so warn again even if it is short.
                    rt.cut_armed = rt.warn_armed = True
                elif left is None or left[0] > warn_s + 60:  # margin: no flapping at the line
                    rt.warn_armed = True
            send = dev.notify_enabled and rt.online and bool(rt.ip)
            if due and rt.warn_armed:
                rt.warn_armed = False
                if send:
                    self.outbox.append(self._warn_notice(dev, rt, left))
            if cut and rt.cut_armed:
                rt.cut_armed = False
                if send:
                    self.outbox.append(self._cut_notice(dev, rt))

    def _warn_notice(self, dev: DeviceRule, rt: Runtime, left: Tuple[int, str]) -> Notice:
        mins = max(1, math.ceil(left[0] / 60))
        yt = dev.yt_only_limit and left[1] != "extension"
        big = f"YouTube 還剩 {mins} 分鐘" if yt else f"還剩 {mins} 分鐘"
        if left[1] == "window":
            sub = f"{dev.name} 可用時段到 {dev.win_end // 60:02d}:{dev.win_end % 60:02d}"
        elif left[1] == "extension":
            sub = f"{dev.name} 延長時間到 {dev.extend_min // 60:02d}:{dev.extend_min % 60:02d}"
        else:
            sub = f"{dev.name} 今天的{'YouTube ' if yt else '上網'}時間"
        return Notice(rt.ip, big, sub)

    def _cut_notice(self, dev: DeviceRule, rt: Runtime) -> Notice:
        if rt.reason in YT_LIMIT:
            return Notice(rt.ip, "YouTube 時間到了", "其他 App 可以繼續使用")
        if rt.reason == Reason.QUOTA:
            return Notice(rt.ip, "時間到了", f"{dev.name} 今天的上網時數已用完")
        return Notice(rt.ip, "時間到了", f"{dev.name} 已超過可使用時段")

    def drain_outbox(self) -> List[Notice]:
        out, self.outbox = self.outbox, []
        return out

    # ----------------------------------------------------------- now playing --

    def now_playing(self, client_ip: str, body: Dict[str, Any]) -> None:
        """A report from the NetFlow TV app: the media sessions on the device,
        each with package, title, artist (the channel, for YouTube), state."""
        mac = self.ip_to_mac.get(client_ip)
        if mac not in self.rt:
            raise ApiError(404, "unknown device")
        sessions = body.get("sessions")
        if not isinstance(sessions, list):
            raise ApiError(400, "bad sessions")
        clean = []
        for s in sessions[:16]:
            if not isinstance(s, dict):
                continue
            item = {k: str(s[k])[:200] for k in ("package", "title", "artist", "album", "mediaId", "state") if s.get(k)}
            for k in ("positionMs", "durationMs"):
                if isinstance(s.get(k), int):
                    item[k] = s[k]
            clean.append(item)
        rt = self.rt[mac]
        before = _playing(rt.now_playing)
        rt.now_playing, rt.now_playing_at = clean, time.time()
        after = _playing(clean)
        # A new video reports "playing" a moment before its title: wait for it.
        if after and after.get("title") and after != before:
            log.info(
                "now playing on %s: %s — %s (%s)",
                self.st.devices[mac].name, after.get("title", "?"), after.get("artist", "?"), after.get("package", "?"),
            )

    def notify_test(self, body: Dict[str, Any]) -> None:
        d = self._device(body)
        rt = self.rt[d.mac]
        if not rt.ip:
            raise ApiError(409, "device offline")
        self.outbox.append(Notice(rt.ip, "通知測試", f"{d.name} 的提醒通知正常"))

    def reset_usage(self) -> None:
        for dev in self.st.devices.values():
            dev.used_sec = dev.yt_used_sec = 0
            dev.up_bytes = dev.down_bytes = 0
            dev.extend_min = dev.extend_day = 0  # a "today" extension does not cross the reset
        self.sync()
        self.save()
        log.info("daily counters reset")

    # ----------------------------------------------------------------- API --

    def status(self) -> Dict[str, Any]:
        now, _ = self._now()
        return {
            "uplinkUp": self.uplink_up,
            "wanIp": _if_addr(self.conf.wan_if),
            "lanIp": self.conf.lan_ip,
            "time": now.strftime("%m/%d %H:%M:%S") if self.time_valid else "",
            "timeValid": self.time_valid,
            "resetMin": self.st.cfg.reset_min,
            "defaultAllow": self.st.cfg.default_allow,
            "activeKBmin": self.st.cfg.active_kbmin,
            "blockEncDns": self.st.cfg.block_enc_dns,
            "online": sum(1 for rt in self.rt.values() if rt.online),
            "ytDetectWarn": sum(1 for rt in self.rt.values() if rt.yt_warn),
            "uptimeSec": int(time.monotonic() - self.started),
        }

    def devices(self) -> Dict[str, Any]:
        _, now_min = self._now()
        out = []
        for mac, d in self.st.devices.items():
            rt = self.rt[mac]
            out.append(
                {
                    "mac": mac.upper(),
                    "name": d.name,
                    "ip": rt.ip,
                    "online": rt.online,
                    "approved": d.approved,
                    "reason": int(rt.reason),
                    "usedSec": d.used_sec,
                    "ytUsedSec": d.yt_used_sec,
                    "winEnabled": d.win_enabled,
                    "winStart": d.win_start,
                    "winEnd": d.win_end,
                    "quotaEnabled": d.quota_enabled,
                    "quotaMin": d.quota_min,
                    "manualBlock": d.manual_block,
                    "blockYoutube": d.block_youtube,
                    "ytOnlyLimit": d.yt_only_limit,
                    "ytDetectWarn": rt.yt_warn,
                    # The playing session, if the device runs the NetFlow TV app.
                    "nowPlaying": _playing(rt.now_playing) if time.time() - rt.now_playing_at < NOW_PLAYING_STALE_SEC else None,
                    "notifyEnabled": d.notify_enabled,
                    "notifyWarnMin": d.notify_warn_min,
                    "up": d.up_bytes,
                    "down": d.down_bytes,
                    # extendUntil: the target if the extension is for today, else 0.
                    "extendUntil": d.extend_min if d.extend_day == self.st.day_key else 0,
                    "extendActive": self.time_valid
                    and rules.extension_active(d, now_min, self.st.day_key, self.st.cfg.reset_min),
                }
            )
        return {"devices": out}

    def _device(self, body: Dict[str, Any]) -> DeviceRule:
        mac = norm_mac(body.get("mac", ""))
        d = self.st.devices.get(mac)
        if d is None:
            raise ApiError(404, "unknown device")
        return d

    def update_device(self, body: Dict[str, Any]) -> None:
        d = self._device(body)
        if body.get("remove") is True:
            del self.st.devices[d.mac]
            del self.rt[d.mac]
        else:
            d.name = str(body.get("name") or d.name)[:32]
            d.approved = bool(body.get("approved", False))
            d.win_enabled = bool(body.get("winEnabled", False))
            d.win_start = clamp_min(body.get("winStart", 0))
            d.win_end = clamp_min(body.get("winEnd", 0))
            d.quota_enabled = bool(body.get("quotaEnabled", False))
            d.quota_min = max(1, min(1440, int(body.get("quotaMin", 480))))
            d.manual_block = bool(body.get("manualBlock", False))
            d.block_youtube = bool(body.get("blockYoutube", False))
            d.yt_only_limit = bool(body.get("ytOnlyLimit", False))
            d.notify_enabled = bool(body.get("notifyEnabled", d.notify_enabled))
            d.notify_warn_min = max(1, min(120, int(body.get("notifyWarnMin", d.notify_warn_min))))
        self.save()
        self.sync()

    def update_global(self, body: Dict[str, Any]) -> None:
        cfg = self.st.cfg
        cfg.reset_min = clamp_min(body.get("resetMin", cfg.reset_min))
        cfg.default_allow = bool(body.get("defaultAllow", True))
        cfg.active_kbmin = max(1, min(60000, int(body.get("activeKBmin", cfg.active_kbmin))))
        cfg.block_enc_dns = bool(body.get("blockEncDns", cfg.block_enc_dns))
        self.save()
        self.sync()

    def extend(self, body: Dict[str, Any]) -> None:
        """{mac, untilMin} extends until that minute, today only; {mac, cancel} clears."""
        d = self._device(body)
        if body.get("cancel") is True:
            d.extend_min = d.extend_day = 0
        else:
            # A target time is meaningless without a clock, and it must be
            # pinned to the current logical day to be a "today" grant.
            if not self.time_valid or self.st.day_key == 0:
                raise ApiError(409, "clock not set")
            until = body.get("untilMin", -1)
            if not isinstance(until, int) or not 0 <= until <= 1439:
                raise ApiError(400, "bad untilMin")
            d.extend_min = until
            d.extend_day = self.st.day_key
        self.save()
        self.sync()


def _playing(sessions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The session worth showing: one that is playing, else none."""
    for s in sessions:
        if s.get("state") in ("playing", "buffering"):
            return s
    return None


def _if_addr(ifname: str) -> str:
    import subprocess

    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", ifname], capture_output=True, text=True)
        return json.loads(r.stdout)[0]["addr_info"][0]["local"]
    except (OSError, ValueError, IndexError, KeyError):
        return ""
