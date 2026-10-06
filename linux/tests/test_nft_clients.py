import unittest

from netflow import clients
from netflow.nft import Policy, parse_counters, policy_script, video_script


class PolicyScript(unittest.TestCase):
    def test_full(self):
        s = policy_script(
            Policy(
                known=frozenset({"aa:00:00:00:00:02", "aa:00:00:00:00:01"}),
                blocked=frozenset({"aa:00:00:00:00:01"}),
                ytblock=frozenset(),
                default_allow=False,
                block_enc_dns=True,
            )
        )
        self.assertIn("add element inet netflow known { aa:00:00:00:00:01, aa:00:00:00:00:02 }", s)
        self.assertIn("flush set inet netflow ytblock", s)
        self.assertNotIn("add element inet netflow ytblock", s)  # empty set: flush only
        self.assertIn("add rule inet netflow unknown jump refuse", s)
        self.assertIn("th dport 853 jump refuse", s)

    def test_permissive(self):
        s = policy_script(Policy(frozenset(), frozenset(), frozenset(), default_allow=True, block_enc_dns=False))
        self.assertNotIn("add rule", s)

    def test_video(self):
        # Keyed by client: the same video address learned by two devices is two pairs.
        pairs = [("192.168.50.101", "1.2.3.4"), ("192.168.50.101", "1.2.3.4"), ("192.168.50.102", "1.2.3.4")]
        s = video_script(pairs)
        elems = "{ 192.168.50.101 . 1.2.3.4, 192.168.50.102 . 1.2.3.4 }"
        self.assertIn(f"destroy element inet netflow ytvideo {elems}", s)
        self.assertIn(f"add element inet netflow ytvideo {elems}", s)
        self.assertNotIn("timeout", s)  # the set's own timeout applies


class Counters(unittest.TestCase):
    def test_parse(self):
        doc = {
            "nftables": [
                {"metainfo": {}},
                {"set": {"name": "acct_up", "elem": [{"elem": {"val": "192.168.50.101", "counter": {"packets": 5, "bytes": 5140}}}]}},
                {"set": {"name": "known", "elem": ["aa:bb:cc:dd:ee:ff"]}},
                {"set": {"name": "acct_down"}},
            ]
        }
        c = parse_counters(doc)
        self.assertEqual(c["acct_up"], {"192.168.50.101": 5140})
        self.assertEqual(c["acct_down"], {})
        self.assertNotIn("known", c)


class Clients(unittest.TestCase):
    def test_leases_and_neigh(self):
        leases = clients.parse_leases(
            "1790000000 1c:53:f9:16:66:68 192.168.50.101 lv-chrome 01:1c:53:f9:16:66:68\n"
            "1790000000 26:2E:19:27:84:D3 192.168.50.102 * *\n"
            "garbage line\n"
        )
        neigh = clients.parse_neigh(
            [
                {"dst": "192.168.50.110", "lladdr": "1c:53:f9:16:66:68", "state": ["REACHABLE"]},
                {"dst": "192.168.50.103", "lladdr": "aa:aa:aa:aa:aa:aa", "state": ["FAILED"]},
                {"dst": "fe80::1", "lladdr": "bb:bb:bb:bb:bb:bb", "state": ["STALE"]},
            ]
        )
        m = clients.merge(leases, neigh)
        self.assertEqual(set(m), {"1c:53:f9:16:66:68", "26:2e:19:27:84:d3"})
        tv = m["1c:53:f9:16:66:68"]
        self.assertEqual((tv.ip, tv.hostname, tv.present), ("192.168.50.110", "lv-chrome", True))
        self.assertFalse(m["26:2e:19:27:84:d3"].present)
        self.assertEqual(clients.default_name(m["26:2e:19:27:84:d3"], "26:2e:19:27:84:d3"), "2784D3")
        self.assertEqual(clients.default_name(tv, tv.mac), "lv-chrome")


if __name__ == "__main__":
    unittest.main()
