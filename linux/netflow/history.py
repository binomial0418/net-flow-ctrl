"""Viewing history: seconds of playback per logical day, device, app, channel
and title, from what the NetFlow TV app reports (see tvapp/).

Seconds are gathered in memory and written in batches (the controller flushes
once a minute and on shutdown), so a busy evening is a handful of SQLite
writes, not one per second."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Tuple

Key = Tuple[int, str, str, str, str]  # day, mac, package, channel, title

# Friendly names for the apps seen on the TVs; anything else shows its package.
APP_NAMES = {
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvkids": "YouTube Kids",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.spotify.tv.android": "Spotify",
    "com.netflix.ninja": "Netflix",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "hami.androidtv": "Hami Video",
    "com.taiwanmobile.myVideotv": "myVideo",
    "com.iqiyi.i18n.tv": "愛奇藝",
    "com.chocolabs.app.chocotv.tv": "LINE TV",
    "tw.com.gamer.android.animad": "動畫瘋",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "org.xbmc.kodi": "Kodi",
    "org.jellyfin.androidtv": "Jellyfin",
    "org.videolan.vlc": "VLC",
}


def app_name(package: str) -> str:
    return APP_NAMES.get(package, package)


def day_minus(day: int, n: int) -> int:
    """The day key n days before `day` (both YYYYMMDD)."""
    d = date(day // 10000, day // 100 % 100, day % 100) - timedelta(days=n)
    return d.year * 10000 + d.month * 100 + d.day


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The daemon is single-threaded; tools that are not serialise access.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS watch (
                 day INTEGER NOT NULL, mac TEXT NOT NULL, package TEXT NOT NULL,
                 channel TEXT NOT NULL, title TEXT NOT NULL,
                 seconds INTEGER NOT NULL, last_ts REAL NOT NULL,
                 PRIMARY KEY (day, mac, package, channel, title))"""
        )
        self.db.commit()
        self._pending: DefaultDict[Key, int] = defaultdict(int)
        self._last_ts: Dict[Key, float] = {}

    def add(self, day: int, mac: str, package: str, channel: str, title: str, secs: int, ts: float) -> None:
        k = (day, mac, package, channel, title)
        self._pending[k] += secs
        self._last_ts[k] = ts

    def flush(self) -> None:
        if not self._pending:
            return
        self.db.executemany(
            """INSERT INTO watch (day, mac, package, channel, title, seconds, last_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (day, mac, package, channel, title)
               DO UPDATE SET seconds = seconds + excluded.seconds, last_ts = excluded.last_ts""",
            [(*k, s, self._last_ts[k]) for k, s in self._pending.items()],
        )
        self.db.commit()
        self._pending.clear()
        self._last_ts.clear()

    def prune(self, before_day: int) -> int:
        cur = self.db.execute("DELETE FROM watch WHERE day < ?", (before_day,))
        self.db.commit()
        return cur.rowcount

    def query(self, since_day: int, mac: Optional[str] = None) -> List[Dict[str, Any]]:
        """Rows since a day (inclusive), unflushed seconds included."""
        self.flush()
        sql = "SELECT day, mac, package, channel, title, seconds, last_ts FROM watch WHERE day >= ?"
        args: List[Any] = [since_day]
        if mac:
            sql += " AND mac = ?"
            args.append(mac)
        cols = ("day", "mac", "package", "channel", "title", "seconds", "last_ts")
        return [dict(zip(cols, r)) for r in self.db.execute(sql, args)]

    def close(self) -> None:
        self.flush()
        self.db.close()


def summarise(rows: List[Dict[str, Any]], names: Dict[str, str]) -> List[Dict[str, Any]]:
    """Per device: total, and channels by time, each with its titles by time."""
    devices: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        dev = devices.setdefault(r["mac"], {"mac": r["mac"].upper(), "name": names.get(r["mac"], r["mac"].upper()),
                                            "seconds": 0, "channels": {}})
        dev["seconds"] += r["seconds"]
        ck = (r["package"], r["channel"])
        ch = dev["channels"].setdefault(ck, {"app": app_name(r["package"]), "package": r["package"],
                                             "channel": r["channel"], "seconds": 0, "lastTs": 0.0, "titles": {}})
        ch["seconds"] += r["seconds"]
        ch["lastTs"] = max(ch["lastTs"], r["last_ts"])
        ch["titles"][r["title"]] = ch["titles"].get(r["title"], 0) + r["seconds"]
    out = []
    for dev in sorted(devices.values(), key=lambda d: -d["seconds"]):
        chans = []
        for ch in sorted(dev["channels"].values(), key=lambda c: -c["seconds"]):
            ch["titles"] = [{"title": t, "seconds": s} for t, s in sorted(ch["titles"].items(), key=lambda kv: -kv[1])]
            chans.append(ch)
        dev["channels"] = chans
        out.append(dev)
    return out
