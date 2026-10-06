"""Just enough DNS wire format to recognise YouTube: read the question name,
answer NXDOMAIN, and pull A records out of googlevideo.com answers.
Ported from esp32/nfc_filter.cpp."""
from __future__ import annotations

import struct
from typing import List, NamedTuple, Optional, Sequence, Set, Tuple

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
# for noticing that video traffic is going unrecognised. The phone app calls
# youtubei.googleapis.com; browsers, and as far as is known the TV app
# (Cobalt), load www.youtube.com / m.youtube.com instead -- not yet confirmed
# on a Google TV; netflow.json's log_queries shows what a device really asks.
YOUTUBE_APP_DOMAINS = ("youtubei.googleapis.com", "www.youtube.com", "m.youtube.com")
# DNS-over-HTTPS/TLS resolvers. A client has to look the resolver up before it
# can use it, so answering NXDOMAIN here catches encrypted DNS whose addresses
# are not in the ruleset's dohips. use-application-dns.net is Firefox's canary:
# NXDOMAIN tells it to keep its DoH off.
ENC_DNS_DOMAINS = (
    "use-application-dns.net",
    "dns.google",
    "dns.google.com",
    "cloudflare-dns.com",
    "one.one.one.one",
    "dns.quad9.net",
    "doh.opendns.com",
    "dns.adguard.com",
    "dns.adguard-dns.com",
    "dns.nextdns.io",
    "doh.cleanbrowsing.org",
)

RCODE_FORMERR = 1
RCODE_NXDOMAIN = 3
TYPE_A = 1
TYPE_CNAME = 5
_MAX_POINTERS = 16  # compression pointers followed in one name: no loops


def norm_domain(name: str) -> str:
    """Domains as configured: lowercase, no surrounding dots or spaces."""
    return name.strip().strip(".").lower()


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


def read_name(msg: bytes, off: int) -> Optional[Tuple[str, int]]:
    """Decode a resource-record name, following compression pointers.
    Returns (name, offset after it in the record), or None when malformed."""
    labels: List[str] = []
    end = None  # where the record continues: just past the first pointer
    jumps = 0
    while off < len(msg):
        n = msg[off]
        if n == 0:
            return ".".join(labels), end if end is not None else off + 1
        if n & 0xC0 == 0xC0:
            if off + 2 > len(msg) or jumps >= _MAX_POINTERS:
                return None
            if end is None:
                end = off + 2
            off = struct.unpack_from("!H", msg, off)[0] & 0x3FFF
            jumps += 1
            continue
        if n > 63 or off + 1 + n > len(msg):
            return None
        labels.append(msg[off + 1 : off + 1 + n].decode("ascii", "replace").lower())
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


def qdcount(msg: bytes) -> int:
    return struct.unpack_from("!H", msg, 4)[0] if len(msg) >= 12 else 0


def _reply_header(query: bytes, rcode: int, qd: int) -> bytes:
    flags1 = 0x80 | (query[2] & 0x79)  # QR, keep opcode and RD
    flags2 = 0x80 | rcode  # RA
    return query[:2] + bytes((flags1, flags2)) + struct.pack("!HHHH", qd, 0, 0, 0)


def nxdomain(query: bytes) -> Optional[bytes]:
    """An NXDOMAIN answer to `query`, echoing its first question."""
    q = question(query)
    if q is None:
        return None
    return _reply_header(query, RCODE_NXDOMAIN, 1) + query[12 : q[1]]


def formerr(query: bytes) -> Optional[bytes]:
    """A FORMERR answer with no question section, for a query that cannot be
    read. None when there is not even a header to answer."""
    if len(query) < 12:
        return None
    return _reply_header(query, RCODE_FORMERR, 0)


class Record(NamedTuple):
    owner: str
    rtype: int
    ttl: int
    rdata_off: int
    rdlen: int


class Answer(NamedTuple):
    """A successful single-question answer: its question name, the names it
    leads to through CNAMEs (the question itself included), and the records
    of the answer section."""

    qname: str
    chain: Set[str]
    records: List[Record]


def parse_answer(resp: bytes) -> Optional[Answer]:
    """The answer section of a successful response to one question, or None.
    A record that cannot be read ends the section; the ones before it stand."""
    if len(resp) < 12 or not resp[2] & 0x80 or resp[3] & 0x0F:
        return None
    qd, an = struct.unpack_from("!HH", resp, 4)
    if qd != 1:
        return None
    q = question(resp)
    if q is None:
        return None
    off = q[1]
    records: List[Record] = []
    for _ in range(an):
        n = read_name(resp, off)
        if n is None or n[1] + 10 > len(resp):
            break
        owner, off = n
        rtype, _cls, ttl, rdlen = struct.unpack_from("!HHIH", resp, off)
        off += 10
        if off + rdlen > len(resp):
            break
        records.append(Record(owner, rtype, ttl, off, rdlen))
        off += rdlen
    # Follow CNAMEs from the question, in whatever order the records came.
    chain = {q[0]}
    cnames = []
    for r in records:
        if r.rtype == TYPE_CNAME:
            t = read_name(resp, r.rdata_off)
            if t is not None:
                cnames.append((r.owner, t[0]))
    grew = True
    while grew:
        grew = False
        for owner, target in cnames:
            if owner in chain and target not in chain:
                chain.add(target)
                grew = True
    return Answer(q[0], chain, records)


def chain_matches(resp: bytes, domains: Sequence[str]) -> bool:
    """Whether an answer leads, through its CNAMEs, to one of `domains`."""
    a = parse_answer(resp)
    return a is not None and any(matches(n, domains) for n in a.chain)


def video_addresses(resp: bytes, video_domains: Sequence[str] = VIDEO_DOMAINS) -> List[Tuple[str, int]]:
    """(ipv4, ttl) for every A record in a successful answer for a video
    domain -- asked for directly, or reached through a CNAME."""
    a = parse_answer(resp)
    if a is None or not any(matches(n, video_domains) for n in a.chain):
        return []
    return [
        (".".join(str(b) for b in resp[r.rdata_off : r.rdata_off + 4]), r.ttl)
        for r in a.records
        if r.rtype == TYPE_A and r.rdlen == 4 and r.owner in a.chain
    ]
