"""Rule evaluation and usage timing -- a straight port of esp32/nfc_state.cpp."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .model import ACTIVE_WINDOW_SEC, DeviceRule, Reason


def in_window(now_min: int, start: int, end: int) -> bool:
    """start == end covers the whole day; start > end wraps midnight."""
    if start == end:
        return True
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def day_key(now: datetime, reset_min: int) -> int:
    """The logical day a counter belongs to: the clock shifted back by the reset
    offset, so the day rolls over exactly at reset_min. Deriving it from the
    wall clock means a restart or a missed reset still lands on the right day."""
    t = now - timedelta(minutes=reset_min)
    return t.year * 10000 + t.month * 100 + t.day


def extension_active(d: DeviceRule, now_min: int, today: int, reset_min: int) -> bool:
    """Compared in logical-day-relative minutes, so an extension into the small
    hours (now 23:00, until 01:00, reset 05:00) is still seen as ahead."""
    if d.extend_day == 0 or d.extend_day != today:
        return False
    now_rel = (now_min + 1440 - reset_min) % 1440
    tgt_rel = (d.extend_min + 1440 - reset_min) % 1440
    return now_rel < tgt_rel


def evaluate(
    d: DeviceRule, now_min: int, *, uplink_up: bool, time_valid: bool, today: int, reset_min: int
) -> Reason:
    if d.manual_block:
        return Reason.MANUAL
    if not d.approved:
        return Reason.UNAPPROVED
    if not uplink_up:
        return Reason.NO_UPLINK
    # A live "extend today" grant beats both limits. It is a target time, so
    # without a clock it reads as inactive and the limits below still apply.
    if time_valid and extension_active(d, now_min, today, reset_min):
        return Reason.ALLOWED
    # The quota is an elapsed-seconds counter, enforced even without a clock.
    if d.quota_enabled and d.used_sec >= d.quota_min * 60:
        return Reason.YT_QUOTA if d.yt_only_limit else Reason.QUOTA
    # A window cannot be judged without the time: fail open.
    if time_valid and d.win_enabled and not in_window(now_min, d.win_start, d.win_end):
        return Reason.YT_WINDOW if d.yt_only_limit else Reason.WINDOW
    return Reason.ALLOWED


class ActivityWindow:
    """Trailing per-second byte totals over `size` seconds (ACTIVE_WINDOW_SEC by
    default). Feed it once a second for every device (0 when idle) so it
    reflects the true trailing period."""

    def __init__(self, size: int = ACTIVE_WINDOW_SEC) -> None:
        self._win = [0] * size
        self._idx = 0
        self.total = 0

    def tick(self, delta_bytes: int, threshold_bytes: int = 0) -> bool:
        """Roll the window; True when it holds at least threshold_bytes."""
        self.total += delta_bytes - self._win[self._idx]
        self._win[self._idx] = delta_bytes
        self._idx = (self._idx + 1) % len(self._win)
        return self.total >= threshold_bytes


def time_left(
    d: DeviceRule, now: datetime, *, time_valid: bool, today: int, reset_min: int
) -> Optional[Tuple[int, str]]:
    """Seconds until a time limit cuts the device and which limit it is
    ("quota", "window" or "extension"); None when no time limit applies.

    The quota is in seconds of use (it only runs while the device is active),
    the window and the extension in wall-clock seconds -- close enough for a
    reminder. While an extension is active nothing can cut before it ends, so
    a limit due earlier is pushed to the extension's end."""
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    limits: List[Tuple[int, str]] = []
    if d.quota_enabled:
        limits.append((max(0, d.quota_min * 60 - d.used_sec), "quota"))
    if time_valid and d.win_enabled and d.win_start != d.win_end:
        if in_window(now.hour * 60 + now.minute, d.win_start, d.win_end):
            limits.append(((d.win_end * 60 - now_s) % 86400, "window"))
        else:
            limits.append((0, "window"))
    if not limits:
        return None
    ext_left = None
    if time_valid and extension_active(d, now.hour * 60 + now.minute, today, reset_min):
        now_rel = (now_s - reset_min * 60) % 86400
        ext_left = (d.extend_min * 60 - reset_min * 60) % 86400 - now_rel
    best: Optional[Tuple[int, str]] = None
    for secs, kind in limits:
        if ext_left is not None and secs <= ext_left:
            secs, kind = ext_left, "extension"
        if best is None or secs < best[0]:
            best = (secs, kind)
    return best
