"""The daemon's handle on the nftables ruleset laid out in deploy/nftables.conf:
it rewrites the policy sets and chains, reads the per-client byte counters, and
records learned video addresses."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

log = logging.getLogger(__name__)

TABLE = "inet netflow"
NFT = "/usr/sbin/nft"
ACCT_SETS = ("acct_up", "acct_down", "acct_yt_up", "acct_yt_down")
# Re-push an unchanged policy this often anyway: a reload of nftables.service
# empties the sets behind our back, which would otherwise leave every device
# refused until something happened to change.
REAPPLY_SEC = 60


@dataclass(frozen=True)
class Policy:
    """Everything the packet path needs to know, as one comparable value."""

    known: FrozenSet[str]
    blocked: FrozenSet[str]
    ytblock: FrozenSet[str]
    default_allow: bool
    block_enc_dns: bool


def _elements(name: str, items: Iterable[str]) -> str:
    items = sorted(items)
    lines = [f"flush set {TABLE} {name}"]
    if items:
        lines.append(f"add element {TABLE} {name} {{ {', '.join(items)} }}")
    return "\n".join(lines)


def policy_script(p: Policy) -> str:
    """One nft batch, applied atomically, that brings the packet path to `p`."""
    parts = [
        _elements("known", p.known),
        _elements("blocked", p.blocked),
        _elements("ytblock", p.ytblock),
        f"flush chain {TABLE} unknown",
    ]
    if not p.default_allow:
        parts.append(f"add rule {TABLE} unknown jump refuse")
    parts.append(f"flush chain {TABLE} encdns")
    if p.block_enc_dns:
        parts.append(f"add rule {TABLE} encdns meta l4proto {{ tcp, udp }} th dport 853 jump refuse")
        parts.append(f"add rule {TABLE} encdns ip daddr @dohips meta l4proto {{ tcp, udp }} th dport 443 jump refuse")
    return "\n".join(parts) + "\n"


def video_script(pairs: Iterable[Tuple[str, str]]) -> str:
    """(Re)insert learned (client, video address) pairs. destroy is a no-op for
    an absent element, and re-adding resets its expiry to the set's timeout;
    traffic on the pair keeps refreshing it from the packet path after that."""
    elems = ", ".join(f"{c} . {v}" for c, v in sorted(set(pairs)))
    return f"destroy element {TABLE} ytvideo {{ {elems} }}\nadd element {TABLE} ytvideo {{ {elems} }}\n"


def parse_counters(doc: dict) -> Dict[str, Dict[str, int]]:
    """{set name: {ip: bytes}} for the accounting sets in `nft -j list table`."""
    out: Dict[str, Dict[str, int]] = {name: {} for name in ACCT_SETS}
    for item in doc.get("nftables", []):
        s = item.get("set")
        if not s or s.get("name") not in out:
            continue
        for e in s.get("elem", []):
            elem = e.get("elem", {})
            ip, ctr = elem.get("val"), elem.get("counter")
            if isinstance(ip, str) and ctr:
                out[s["name"]][ip] = int(ctr.get("bytes", 0))
    return out


class Nft:
    def __init__(self) -> None:
        self._applied: Optional[Policy] = None
        self._applied_at = 0.0

    def _run(self, script: str) -> bool:
        r = subprocess.run([NFT, "-f", "-"], input=script, text=True, capture_output=True)
        if r.returncode != 0:
            log.error("nft failed: %s\n%s", r.stderr.strip(), script)
            return False
        return True

    def apply_policy(self, p: Policy) -> None:
        if p == self._applied and time.monotonic() - self._applied_at < REAPPLY_SEC:
            return
        if self._run(policy_script(p)):
            self._applied = p
            self._applied_at = time.monotonic()

    def counters(self) -> Dict[str, Dict[str, int]]:
        r = subprocess.run([NFT, "-j", "list", "table", *TABLE.split()], capture_output=True, text=True)
        if r.returncode != 0:
            log.error("nft list failed: %s", r.stderr.strip())
            return {name: {} for name in ACCT_SETS}
        return parse_counters(json.loads(r.stdout))

    def sync_video(self, pairs: Iterable[Tuple[str, str]]) -> None:
        """(Re)insert pairs from the main loop, refreshing their timeout."""
        self._run(video_script(pairs))

    async def add_video(self, pairs: Iterable[Tuple[str, str]]) -> None:
        proc = await asyncio.create_subprocess_exec(
            NFT, "-f", "-", stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate(video_script(pairs).encode())
        if proc.returncode != 0:
            log.error("nft video add failed: %s", err.decode().strip())
