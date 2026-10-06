"""JSON persistence. Writes go to a temp file, are fsync'd, then renamed over
the old file, so a power cut leaves either the old or the new state -- never a
half-written one."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .model import State, state_from_dict, state_to_dict

log = logging.getLogger(__name__)


def load(path: Path) -> State:
    try:
        with open(path, encoding="utf-8") as f:
            return state_from_dict(json.load(f))
    except FileNotFoundError:
        return State()
    except (OSError, ValueError) as e:
        # Keep the unreadable file for inspection rather than overwriting it.
        bad = path.with_suffix(path.suffix + ".bad")
        log.error("state file unreadable (%s), moved to %s, starting fresh", e, bad)
        try:
            os.replace(path, bad)
        except OSError:
            pass
        return State()


def save(path: Path, st: State) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state_to_dict(st), f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)  # make the rename itself durable
    finally:
        os.close(dfd)
