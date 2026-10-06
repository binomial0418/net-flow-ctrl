import random
import struct
import unittest

from netflow import dnsmsg


def qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def query(name: str, qid: int = 0x1234) -> bytes:
    return struct.pack("!HBBHHHH", qid, 0x01, 0x00, 1, 0, 0, 0) + qname(name) + struct.pack("!HH", 1, 1)


def answer(name: str, records) -> bytes:
    """records: list of (type, rdata), owned by the question (a compression
    pointer to it), or (owner, type, rdata) with an owner name of their own."""
    msg = struct.pack("!HBBHHHH", 0x1234, 0x81, 0x80, 1, len(records), 0, 0) + qname(name) + struct.pack("!HH", 1, 1)
    for rec in records:
        owner, rtype, rdata = rec if len(rec) == 3 else (None, *rec)
        msg += (qname(owner) if owner else b"\xc0\x0c") + struct.pack("!HHIH", rtype, 1, 300, len(rdata)) + rdata
    return msg


def cname_answer(name: str, target: str, ip: bytes) -> bytes:
    """name CNAME target (written uncompressed), target A ip (owner a pointer to the CNAME's rdata)."""
    head = struct.pack("!HBBHHHH", 0x1234, 0x81, 0x80, 1, 2, 0, 0) + qname(name) + struct.pack("!HH", 1, 1)
    cname = b"\xc0\x0c" + struct.pack("!HHIH", 5, 1, 300, len(qname(target)))
    target_off = len(head) + len(cname)
    a = struct.pack("!H", 0xC000 | target_off) + struct.pack("!HHIH", 1, 1, 60, 4) + ip
    return head + cname + qname(target) + a


class Names(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(dnsmsg.is_youtube_name("www.youtube.com"))
        self.assertTrue(dnsmsg.is_youtube_name("rr1---sn-abc.googlevideo.com"))
        self.assertTrue(dnsmsg.is_youtube_name("youtu.be"))
        self.assertFalse(dnsmsg.is_youtube_name("notyoutube.com"))
        self.assertFalse(dnsmsg.is_youtube_name("www.google.com"))
        self.assertFalse(dnsmsg.is_youtube_name("lh3.ggpht.com"))

    def test_trailing_dot_and_case_normalised(self):
        self.assertEqual(dnsmsg.norm_domain(" YouTube.com. "), "youtube.com")

    def test_app_domains_cover_tv_and_phone(self):
        for n in ("youtubei.googleapis.com", "www.youtube.com", "m.youtube.com"):
            self.assertTrue(dnsmsg.matches(n, dnsmsg.YOUTUBE_APP_DOMAINS), n)
        self.assertFalse(dnsmsg.matches("i.ytimg.com", dnsmsg.YOUTUBE_APP_DOMAINS))

    def test_enc_dns_names(self):
        for n in ("dns.google", "mozilla.cloudflare-dns.com", "use-application-dns.net", "dns.quad9.net"):
            self.assertTrue(dnsmsg.matches(n, dnsmsg.ENC_DNS_DOMAINS), n)
        self.assertFalse(dnsmsg.matches("www.google.com", dnsmsg.ENC_DNS_DOMAINS))

    def test_question_lowercased(self):
        self.assertEqual(dnsmsg.question(query("WWW.YouTube.COM"))[0], "www.youtube.com")


class Nxdomain(unittest.TestCase):
    def test_reply(self):
        q = query("www.youtube.com")
        r = dnsmsg.nxdomain(q)
        self.assertEqual(r[:2], q[:2])
        self.assertTrue(r[2] & 0x80)  # QR
        self.assertTrue(r[2] & 0x01)  # RD kept
        self.assertEqual(r[3] & 0x0F, 3)  # NXDOMAIN
        self.assertEqual(struct.unpack_from("!HHHH", r, 4), (1, 0, 0, 0))
        self.assertEqual(r[12:], q[12:])

    def test_drops_additional_section(self):
        q = query("youtube.com") + b"\x00\x00\x29\x10\x00\x00\x00\x00\x00\x00\x00"  # EDNS OPT
        q = q[:10] + b"\x00\x01" + q[12:]
        r = dnsmsg.nxdomain(q)
        self.assertEqual(struct.unpack_from("!H", r, 10)[0], 0)
        self.assertEqual(len(r), len(query("youtube.com")))


class Formerr(unittest.TestCase):
    def test_reply(self):
        q = query("www.youtube.com")
        r = dnsmsg.formerr(q)
        self.assertEqual(r[:2], q[:2])
        self.assertTrue(r[2] & 0x80)
        self.assertEqual(r[3] & 0x0F, 1)
        self.assertEqual(struct.unpack_from("!HHHH", r, 4), (0, 0, 0, 0))
        self.assertEqual(len(r), 12)
        self.assertIsNone(dnsmsg.formerr(b"\x12"))


class ReadName(unittest.TestCase):
    def test_pointer(self):
        msg = qname("rr1.googlevideo.com") + b"\x03www" + b"\xc0\x04"
        self.assertEqual(dnsmsg.read_name(msg, 21), ("www.googlevideo.com", 27))

    def test_pointer_loop_is_rejected(self):
        self.assertIsNone(dnsmsg.read_name(b"\x01a\xc0\x00", 0))


class VideoAddresses(unittest.TestCase):
    def test_learns_a_records_after_cname(self):
        r = answer("rr3---sn-x.googlevideo.com", [(5, b"\xc0\x0c"), (1, bytes([173, 194, 9, 9]))])
        self.assertEqual(dnsmsg.video_addresses(r), [("173.194.9.9", 300)])

    def test_learns_through_a_cname_into_the_video_domain(self):
        # Asked for a name outside the list; the CNAME leads into googlevideo.com.
        r = cname_answer("cdn-alias.example.net", "rr5---sn-x.googlevideo.com", bytes([173, 194, 9, 10]))
        self.assertEqual(dnsmsg.video_addresses(r), [("173.194.9.10", 60)])
        self.assertTrue(dnsmsg.chain_matches(r, dnsmsg.YOUTUBE_DOMAINS))
        self.assertEqual(dnsmsg.parse_answer(r).chain, {"cdn-alias.example.net", "rr5---sn-x.googlevideo.com"})

    def test_cname_chain_in_any_order(self):
        r = answer(
            "a.example.net",
            [
                ("b.example.net", 1, bytes([9, 9, 9, 9])),
                ("b.example.net", 5, qname("rr1.googlevideo.com")),
                ("a.example.net", 5, qname("b.example.net")),
                ("rr1.googlevideo.com", 1, bytes([1, 1, 1, 1])),
            ],
        )
        self.assertEqual(sorted(dnsmsg.video_addresses(r)), [("1.1.1.1", 300), ("9.9.9.9", 300)])

    def test_records_off_the_chain_are_not_learned(self):
        # An unrelated record riding along in the answer section.
        r = answer("rr1.googlevideo.com", [(1, bytes([1, 2, 3, 4])), ("evil.example", 1, bytes([6, 6, 6, 6]))])
        self.assertEqual(dnsmsg.video_addresses(r), [("1.2.3.4", 300)])

    def test_cname_away_from_youtube_is_not_youtube(self):
        r = cname_answer("www.example.com", "edge.example-cdn.net", bytes([5, 5, 5, 5]))
        self.assertEqual(dnsmsg.video_addresses(r), [])
        self.assertFalse(dnsmsg.chain_matches(r, dnsmsg.YOUTUBE_DOMAINS))

    def test_ignores_other_names_and_errors(self):
        self.assertEqual(dnsmsg.video_addresses(answer("www.youtube.com", [(1, bytes(4))])), [])
        r = bytearray(answer("rr3.googlevideo.com", [(1, bytes(4))]))
        r[3] |= 3  # NXDOMAIN
        self.assertEqual(dnsmsg.video_addresses(bytes(r)), [])
        self.assertEqual(dnsmsg.video_addresses(query("rr3.googlevideo.com")), [])  # a query, not an answer

    def test_truncated_and_garbage_never_raise(self):
        r = answer("rr3.googlevideo.com", [(1, bytes([1, 2, 3, 4]))])
        c = cname_answer("cdn-alias.example.net", "rr5.googlevideo.com", bytes(4))
        for n in range(len(c)):
            dnsmsg.video_addresses(c[:n])
            dnsmsg.chain_matches(c[:n], dnsmsg.YOUTUBE_DOMAINS)
        for n in range(len(r)):
            dnsmsg.video_addresses(r[:n])
            dnsmsg.question(r[:n])
            dnsmsg.nxdomain(r[:n])
        rnd = random.Random(1)
        for _ in range(20000):
            junk = bytes(rnd.getrandbits(8) for _ in range(rnd.randrange(0, 80)))
            dnsmsg.video_addresses(junk)
            dnsmsg.chain_matches(junk, dnsmsg.YOUTUBE_DOMAINS)
            dnsmsg.nxdomain(junk)
            dnsmsg.formerr(junk)
            dnsmsg.read_name(junk, 0)


if __name__ == "__main__":
    unittest.main()
