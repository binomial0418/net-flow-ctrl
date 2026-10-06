"""Persisted rules and settings, plus the verdict a device is given.

Field meanings mirror the ESP32 edition (esp32/nfc_config.h) so the two
behave the same; only the storage format differs (JSON instead of NVS blobs).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict

# Usage time accrues only while a device is actually moving data: a second
# counts when the trailing ACTIVE_WINDOW_SEC of traffic reaches the threshold.
# The window is exactly 60 s, so the threshold reads naturally as KB per minute.
ACTIVE_WINDOW_SEC = 60
ACTIVE_KBMIN_DEFAULT = 200


class Reason(IntEnum):
    """Why a device is (partly) cut off. Values are shared with the web page."""

    ALLOWED = 0
    MANUAL = 1
    WINDOW = 2
    QUOTA = 3
    NO_UPLINK = 4
    UNAPPROVED = 5
    # A limit tripped on a device with yt_only_limit: only YouTube is cut.
    YT_WINDOW = 6
    YT_QUOTA = 7


FULL_BLOCK = {Reason.MANUAL, Reason.WINDOW, Reason.QUOTA, Reason.UNAPPROVED}
YT_LIMIT = {Reason.YT_WINDOW, Reason.YT_QUOTA}


@dataclass
class DeviceRule:
    mac: str  # lowercase aa:bb:cc:dd:ee:ff
    name: str = ""
    approved: bool = True
    win_enabled: bool = False  # limit 1: allowed time window
    win_start: int = 6 * 60  # minutes from midnight
    win_end: int = 21 * 60
    quota_enabled: bool = False  # limit 2: daily cumulative minutes
    quota_min: int = 480
    manual_block: bool = False  # admin override, survives the daily reset
    used_sec: int = 0  # consumed today
    up_bytes: int = 0  # today, informational
    down_bytes: int = 0
    # "Extend today until" override: beats both the window and the quota while
    # active. extend_day pins it to one logical day, so it lapses at the reset.
    extend_min: int = 0
    extend_day: int = 0  # day key it applies to; 0 = no extension
    block_youtube: bool = False  # cut YouTube at all times
    yt_only_limit: bool = False  # a tripped limit cuts only YouTube
    yt_used_sec: int = 0  # seconds today spent streaming YouTube video


@dataclass
class GlobalCfg:
    reset_min: int = 5 * 60  # daily reset, minutes from midnight
    default_allow: bool = True  # policy for devices not yet registered
    active_kbmin: int = ACTIVE_KBMIN_DEFAULT  # usage-time traffic threshold
    block_enc_dns: bool = True  # refuse DoT/DoH so every lookup stays visible


@dataclass
class State:
    cfg: GlobalCfg = field(default_factory=GlobalCfg)
    devices: Dict[str, DeviceRule] = field(default_factory=dict)  # keyed by mac
    day_key: int = 0  # logical day the counters belong to


def _from_dict(cls, data: Dict[str, Any]):
    """Build a dataclass, ignoring unknown keys and defaulting missing ones, so
    state written by an older or newer release still loads."""
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def state_to_dict(st: State) -> Dict[str, Any]:
    return {
        "cfg": dataclasses.asdict(st.cfg),
        "devices": [dataclasses.asdict(d) for d in st.devices.values()],
        "day_key": st.day_key,
    }


def state_from_dict(data: Dict[str, Any]) -> State:
    st = State(cfg=_from_dict(GlobalCfg, data.get("cfg", {})), day_key=int(data.get("day_key", 0)))
    for d in data.get("devices", []):
        if "mac" in d:
            dev = _from_dict(DeviceRule, d)
            st.devices[dev.mac] = dev
    return st
