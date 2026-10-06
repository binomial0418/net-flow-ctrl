import random
import struct
import unittest

from netflow import dnsmsg


def qname(name: str) -> bytes:
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def query(name: str, qid: int = 0x1234) -> bytes:
    return struct.pack("!HBBHHHH", qid, 0x01, 0x00, 1, 0, 0, 0) + qname(name) + struct.pack("!HH", 1, 1)


def answer(name: str, records) -> bytes:
    """records: list of (type, rdata). Names are compression pointers to the question."""
    msg = struct.pack("!HBBHHHH", 0x1234, 0x81, 0x80, 1, len(records), 0, 0) + qname(name) + struct.pack("!HH", 1, 1)
    for rtype, rdata in records:
        msg += b"\xc0\x0c" + struct.pack("!HHIH", rtype, 1, 300, len(rdata)) + rdata
    return msg


class Names(unittest.TestCase):
    def test_matching(self):
        self.assertTrue(dnsmsg.is_youtube_name("www.youtube.com"))
        self.assertTrue(dnsmsg.is_youtube_name("rr1---sn-abc.googlevideo.com"))
        self.assertTrue(dnsmsg.is_youtube_name("youtu.be"))
        self.assertFalse(dnsmsg.is_youtube_name("notyoutube.com"))
        self.assertFalse(dnsmsg.is_youtube_name("www.google.com"))
        self.assertFalse(dnsmsg.is_youtube_name("lh3.ggpht.com"))

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


class VideoAddresses(unittest.TestCase):
    def test_learns_a_records_after_cname(self):
        r = answer("rr3---sn-x.googlevideo.com", [(5, b"\xc0\x0c"), (1, bytes([173, 194, 9, 9]))])
        self.assertEqual(dnsmsg.video_addresses(r), [("173.194.9.9", 300)])

    def test_ignores_other_names_and_errors(self):
        self.assertEqual(dnsmsg.video_addresses(answer("www.youtube.com", [(1, bytes(4))])), [])
        r = bytearray(answer("rr3.googlevideo.com", [(1, bytes(4))]))
        r[3] |= 3  # NXDOMAIN
        self.assertEqual(dnsmsg.video_addresses(bytes(r)), [])
        self.assertEqual(dnsmsg.video_addresses(query("rr3.googlevideo.com")), [])  # a query, not an answer

    def test_truncated_and_garbage_never_raise(self):
        r = answer("rr3.googlevideo.com", [(1, bytes([1, 2, 3, 4]))])
        for n in range(len(r)):
            dnsmsg.video_addresses(r[:n])
            dnsmsg.question(r[:n])
            dnsmsg.nxdomain(r[:n])
        rnd = random.Random(1)
        for _ in range(20000):
            junk = bytes(rnd.getrandbits(8) for _ in range(rnd.randrange(0, 80)))
            dnsmsg.video_addresses(junk)
            dnsmsg.nxdomain(junk)


if __name__ == "__main__":
    unittest.main()
