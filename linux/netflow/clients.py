"""Who is on the TV network. dnsmasq's lease file gives hostnames; the kernel
neighbour table gives live IP<->MAC bindings and presence. The AP is a plain
bridge, so client MACs arrive intact."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

ABSENT_STATES = {"FAILED", "INCOMPLETE", "NOARP"}


@dataclass
class Client:
    mac: str
    ip: str = ""
    hostname: str = ""
    present: bool = False  # bound in the neighbour table right now


def parse_leases(text: str) -> Dict[str, Client]:
    """dnsmasq lease lines: '<expiry> <mac> <ip> <hostname|*> <client-id>'."""
    out: Dict[str, Client] = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4 or f[1].count(":") != 5:
            continue
        mac = f[1].lower()
        out[mac] = Client(mac=mac, ip=f[2], hostname="" if f[3] == "*" else f[3])
    return out


def parse_neigh(doc: list) -> Dict[str, Client]:
    """`ip -j neigh show dev <lan>` -> {mac: Client} for live IPv4 bindings."""
    out: Dict[str, Client] = {}
    for n in doc:
        mac, ip = n.get("lladdr"), n.get("dst", "")
        states = set(n.get("state", []))
        if not mac or ":" in ip or states & ABSENT_STATES:
            continue
        out[mac.lower()] = Client(mac=mac.lower(), ip=ip, present=True)
    return out


def merge(leases: Dict[str, Client], neigh: Dict[str, Client]) -> Dict[str, Client]:
    out = {mac: Client(mac=mac, ip=c.ip, hostname=c.hostname) for mac, c in leases.items()}
    for mac, n in neigh.items():
        c = out.setdefault(mac, Client(mac=mac))
        c.ip = n.ip  # the live binding beats the lease
        c.present = True
    return out


def snapshot(lan_if: str, leases_path: Path) -> Dict[str, Client]:
    try:
        leases = parse_leases(leases_path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        leases = {}
    r = subprocess.run(["ip", "-j", "neigh", "show", "dev", lan_if], capture_output=True, text=True)
    neigh = parse_neigh(json.loads(r.stdout or "[]")) if r.returncode == 0 else {}
    return merge(leases, neigh)


def default_name(c: Optional[Client], mac: str) -> str:
    if c and c.hostname:
        return c.hostname[:32]
    return mac.replace(":", "")[-6:].upper()
