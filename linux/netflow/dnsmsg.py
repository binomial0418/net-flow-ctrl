"""Just enough DNS wire format to recognise YouTube: read the question name,
answer NXDOMAIN, and pull A records out of googlevideo.com answers.
Ported from esp32/nfc_filter.cpp."""
from __future__ import annotations

import struct
from typing import List, Optional, Sequence, Tuple

# Defaults for the three domain lists; each can be overridden in netflow.json
# should YouTube move. A name matches when it is one of these or a subdomain of
# one. Kept tight: broader Google domains (googleapis.com, ggpht.com) carry
# unrelated services.
YOUTUBE_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "yt.be",
    "googlevideo.com",
    "ytimg.com",
    "youtubekids.com",
    "youtube-nocookie.com",
    "youtubei.googleapis.com",
    "youtube.googleapis.com",
    "yt3.ggpht.com",
    "yt4.ggpht.com",
)
# The video CDN: its addresses are timed and cut by destination.
VIDEO_DOMAINS = ("googlevideo.com",)
# The YouTube apps' own API. Lookups for it mean the app is in use -- the cue
# for noticing that video traffic is going unrecognised.
YOUTUBE_APP_DOMAINS = ("youtubei.googleapis.com",)


def name_under(name: str, dom: str) -> bool:
    return name == dom or name.endswith("." + dom)


def matches(name: str, domains: Sequence[str]) -> bool:
    return any(name_under(name, d) for d in domains)


def is_youtube_name(name: str, domains: Sequence[str] = YOUTUBE_DOMAINS) -> bool:
    return matches(name, domains)


def read_qname(msg: bytes, off: int) -> Optional[Tuple[str, int]]:
    """Decode an uncompressed question name to lowercase dotted text.
    Returns (name, offset after it), or None when malformed."""
    labels = []
    while off < len(msg):
        n = msg[off]
        off += 1
        if n == 0:
            return ".".join(labels), off
        if n > 63 or off + n > len(msg):
            return None
        labels.append(msg[off : off + n].decode("ascii", "replace").lower())
        off += n
    return None


def skip_name(msg: bytes, off: int) -> Optional[int]:
    """Skip a resource-record name, which may end in a compression pointer."""
    while off < len(msg):
        n = msg[off]
        if n == 0:
            return off + 1
        if n & 0xC0 == 0xC0:
            return off + 2 if off + 2 <= len(msg) else None
        if n > 63:
            return None
        off += 1 + n
    return None


def question(msg: bytes) -> Optional[Tuple[str, int]]:
    """(name, offset past QTYPE/QCLASS) of the first question, or None."""
    if len(msg) < 12 or struct.unpack_from("!H", msg, 4)[0] == 0:
        return None
    q = read_qname(msg, 12)
    if q is None or q[1] + 4 > len(msg):
        return None
    return q[0], q[1] + 4


def is_query(msg: bytes) -> bool:
    return len(msg) >= 12 and not msg[2] & 0x80


def nxdomain(query: bytes) -> Optional[bytes]:
    """An NXDOMAIN answer to `query`, echoing its first question."""
    q = question(query)
    if q is None:
        return None
    flags1 = 0x80 | (query[2] & 0x79)  # QR, keep opcode and RD
    flags2 = 0x80 | 3  # RA, NXDOMAIN
    return query[:2] + bytes((flags1, flags2)) + struct.pack("!HHHH", 1, 0, 0, 0) + query[12 : q[1]]


def video_addresses(resp: bytes, video_domains: Sequence[str] = VIDEO_DOMAINS) -> List[Tuple[str, int]]:
    """(ipv4, ttl) for every A record in a successful answer for a video domain."""
    if len(resp) < 12 or not resp[2] & 0x80 or resp[3] & 0x0F:
        return []
    qd, an = struct.unpack_from("!HH", resp, 4)
    if qd != 1:
        return []
    q = question(resp)
    if q is None or not matches(q[0], video_domains):
        return []
    off = q[1]
    out = []
    for _ in range(an):
        off = skip_name(resp, off)
        if off is None or off + 10 > len(resp):
            break
        rtype, _cls, ttl, rdlen = struct.unpack_from("!HHIH", resp, off)
        off += 10
        if off + rdlen > len(resp):
            break
        if rtype == 1 and rdlen == 4:
            out.append((".".join(str(b) for b in resp[off : off + 4]), ttl))
        off += rdlen
    return out
