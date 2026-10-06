import unittest
from datetime import datetime

from netflow.model import DeviceRule, Reason
from netflow.rules import ActivityWindow, day_key, evaluate, extension_active, in_window

RESET = 5 * 60


def ev(d, now_min, **kw):
    args = dict(uplink_up=True, time_valid=True, today=20260717, reset_min=RESET)
    args.update(kw)
    return evaluate(d, now_min, **args)


class InWindow(unittest.TestCase):
    def test_plain(self):
        self.assertTrue(in_window(6 * 60, 6 * 60, 21 * 60))
        self.assertFalse(in_window(21 * 60, 6 * 60, 21 * 60))

    def test_wraps_midnight(self):
        self.assertTrue(in_window(23 * 60, 22 * 60, 2 * 60))
        self.assertTrue(in_window(60, 22 * 60, 2 * 60))
        self.assertFalse(in_window(3 * 60, 22 * 60, 2 * 60))

    def test_equal_is_whole_day(self):
        self.assertTrue(in_window(123, 300, 300))


class DayKey(unittest.TestCase):
    # The table in esp32/README.md, row by row.
    def test_reset_boundaries(self):
        cases = [
            (datetime(2026, 7, 17, 4, 59), 20260716),
            (datetime(2026, 7, 17, 5, 0), 20260717),
            (datetime(2026, 7, 17, 23, 59), 20260717),
            (datetime(2026, 7, 18, 4, 59), 20260717),
            (datetime(2026, 7, 18, 5, 0), 20260718),
        ]
        for now, key in cases:
            self.assertEqual(day_key(now, RESET), key, now)


class Extension(unittest.TestCase):
    def test_into_small_hours(self):
        d = DeviceRule(mac="m", extend_min=60, extend_day=20260717)
        self.assertTrue(extension_active(d, 23 * 60, 20260717, RESET))
        self.assertTrue(extension_active(d, 30, 20260717, RESET))
        self.assertFalse(extension_active(d, 61, 20260717, RESET))

    def test_lapses_next_day(self):
        d = DeviceRule(mac="m", extend_min=22 * 60, extend_day=20260716)
        self.assertFalse(extension_active(d, 21 * 60, 20260717, RESET))


class Evaluate(unittest.TestCase):
    def test_order(self):
        d = DeviceRule(mac="m", manual_block=True, approved=False)
        self.assertEqual(ev(d, 600), Reason.MANUAL)
        d.manual_block = False
        self.assertEqual(ev(d, 600), Reason.UNAPPROVED)
        d.approved = True
        self.assertEqual(ev(d, 600, uplink_up=False), Reason.NO_UPLINK)
        self.assertEqual(ev(d, 600), Reason.ALLOWED)

    def test_quota_enforced_without_clock(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=1, used_sec=60)
        self.assertEqual(ev(d, 600, time_valid=False), Reason.QUOTA)

    def test_window_fails_open_without_clock(self):
        d = DeviceRule(mac="m", win_enabled=True, win_start=6 * 60, win_end=21 * 60)
        self.assertEqual(ev(d, 22 * 60), Reason.WINDOW)
        self.assertEqual(ev(d, 22 * 60, time_valid=False), Reason.ALLOWED)

    def test_yt_only_limit(self):
        d = DeviceRule(mac="m", yt_only_limit=True, quota_enabled=True, quota_min=1, used_sec=60)
        self.assertEqual(ev(d, 600), Reason.YT_QUOTA)
        d.used_sec = 0
        d.win_enabled, d.win_start, d.win_end = True, 6 * 60, 21 * 60
        self.assertEqual(ev(d, 22 * 60), Reason.YT_WINDOW)

    def test_extension_beats_limits_but_not_manual(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=1, used_sec=999, extend_min=23 * 60, extend_day=20260717)
        self.assertEqual(ev(d, 22 * 60), Reason.ALLOWED)
        self.assertEqual(ev(d, 22 * 60, time_valid=False), Reason.QUOTA)
        d.manual_block = True
        self.assertEqual(ev(d, 22 * 60), Reason.MANUAL)


class Window(unittest.TestCase):
    def test_threshold_and_drain(self):
        w = ActivityWindow()
        th = 200 * 1024
        self.assertFalse(w.tick(100 * 1024, th))
        self.assertTrue(w.tick(100 * 1024, th))
        for _ in range(58):
            self.assertTrue(w.tick(0, th))  # both samples still inside the 60 s window
        self.assertFalse(w.tick(0, th))  # the first sample ages out: below threshold


if __name__ == "__main__":
    unittest.main()
