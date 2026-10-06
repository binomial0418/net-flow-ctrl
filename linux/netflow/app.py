"""The once-a-second control loop and the API it serves. Mirrors the tick in
esp32/esp32.ino: register clients, roll the day, collect bytes, accrue usage,
evaluate rules and push the verdicts to nftables."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import clients, dnsmsg, rules, store
from .model import FULL_BLOCK, YT_LIMIT, DeviceRule, Reason
from .nft import Policy

log = logging.getLogger(__name__)

USAGE_SAVE_SEC = 60  # periodic checkpoint of the usage counters
ONLINE_GRACE_SEC = 120  # recent traffic keeps a device "online" past its neighbour entry

# YouTube recognition health. Recognition hangs on seeing the DNS lookups; a
# client that finds a way around them (a new encrypted-DNS endpoint, say) would
# silently escape both timing and blocking. The tell: the YouTube app is in
# use and plenty of data is moving, yet almost none of it is recognised video.
# Judged over a trailing HEALTH_WINDOW_SEC, raised only once that has held for
# HEALTH_RAISE_SEC, and cleared after HEALTH_CLEAR_SEC without it, so a brief
# overlap (the app opened, then another streaming app) does not flash a warning.
HEALTH_WINDOW_SEC = 300
HEALTH_MIN_BYTES = 20 * 1024 * 1024
HEALTH_MIN_APP_LOOKUPS = 2
HEALTH_MAX_YT_SHARE = 0.05
HEALTH_RAISE_SEC = 120
HEALTH_CLEAR_SEC = 300


@dataclass
class Conf:
    lan_if: str = "ens19"
    wan_if: str = "ens18"
    lan_ip: str = "192.168.50.1"
    http_port: int = 80
    upstream_dns: List[str] = field(default_factory=lambda: ["8.8.8.8", "1.1.1.1", "168.95.192.1"])
    state_path: str = "/var/lib/netflow/state.json"
    leases_path: str = "/var/lib/misc/dnsmasq.leases"
    video_timeout_s: int = 6 * 3600
    max_devices: int = 64
    # Should YouTube move, these can be overridden without touching the code.
    youtube_domains: List[str] = field(default_factory=lambda: list(dnsmsg.YOUTUBE_DOMAINS))
    video_domains: List[str] = field(default_factory=lambda: list(dnsmsg.VIDEO_DOMAINS))
    youtube_app_domains: List[str] = field(default_factory=lambda: list(dnsmsg.YOUTUBE_APP_DOMAINS))

    @classmethod
    def load(cls, path: Path) -> "Conf":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


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
    # Recognition health (see HEALTH_*).
    long_all: rules.ActivityWindow = field(default_factory=lambda: rules.ActivityWindow(HEALTH_WINDOW_SEC))
    long_yt: rules.ActivityWindow = field(default_factory=lambda: rules.ActivityWindow(HEALTH_WINDOW_SEC))
    app_lookups: List[float] = field(default_factory=list)  # monotonic times
    suspect_sec: int = 0  # consecutive seconds the tell has held
    clear_sec: int = 0  # consecutive seconds it has not
    yt_warn: bool = False


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
        self.rt: Dict[str, Runtime] = {mac: Runtime() for mac in self.st.devices}
        self.ip_to_mac: Dict[str, str] = {}
        self.deltas = CounterDeltas()
        self.uplink_up = False
        self.time_valid = False
        self.started = time.monotonic()
        self._last_save = time.monotonic()

    # ------------------------------------------------------------- helpers --

    def _now(self) -> Tuple[datetime, int]:
        now = self.clock()
        return now, now.hour * 60 + now.minute

    def save(self) -> None:
        store.save(Path(self.conf.state_path), self.st)
        self._last_save = time.monotonic()

    def is_yt_blocked(self, client_ip: str) -> bool:
        mac = self.ip_to_mac.get(client_ip)
        d = self.st.devices.get(mac) if mac else None
        if d is None:
            return False
        return d.block_youtube or self.rt[mac].reason in YT_LIMIT

    def note_query(self, client_ip: str, name: str) -> None:
        """Called by the DNS proxy for every lookup."""
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
        self.sync()  # after accruing, so a device that just ran out is cut this tick
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
                self.rt[mac] = Runtime()
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
            self.reset_usage()

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
        rt.app_lookups = [t for t in rt.app_lookups if mono - t < HEALTH_WINDOW_SEC]
        # Under a YouTube cut there is meant to be no video traffic.
        tell = (
            not (dev.block_youtube or rt.reason in YT_LIMIT)
            and len(rt.app_lookups) >= HEALTH_MIN_APP_LOOKUPS
            and rt.long_all.total >= HEALTH_MIN_BYTES
            and rt.long_yt.total < rt.long_all.total * HEALTH_MAX_YT_SHARE
        )
        if tell:
            rt.suspect_sec += 1
            rt.clear_sec = 0
        else:
            rt.clear_sec += 1
            rt.suspect_sec = 0
        if not rt.yt_warn and rt.suspect_sec >= HEALTH_RAISE_SEC:
            rt.yt_warn = True
            log.warning(
                "%s (%s): YouTube app in use but its video is not being recognised -- "
                "DNS may be bypassing the proxy", dev.name, mac
            )
        elif rt.yt_warn and rt.clear_sec >= HEALTH_CLEAR_SEC:
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


def _if_addr(ifname: str) -> str:
    import subprocess

    try:
        r = subprocess.run(["ip", "-j", "-4", "addr", "show", "dev", ifname], capture_output=True, text=True)
        return json.loads(r.stdout)[0]["addr_info"][0]["local"]
    except (OSError, ValueError, IndexError, KeyError):
        return ""
