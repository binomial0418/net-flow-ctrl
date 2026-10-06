import unittest
from datetime import datetime

from netflow.model import DeviceRule, Reason
from netflow.rules import ActivityWindow, day_key, evaluate, extension_active, in_window, time_left

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


class TimeLeft(unittest.TestCase):
    DAY = 20260717

    def left(self, d, h, m, s=0):
        return time_left(d, datetime(2026, 7, 17, h, m, s), time_valid=True, today=self.DAY, reset_min=RESET)

    def test_no_limits(self):
        self.assertIsNone(self.left(DeviceRule(mac="m"), 12, 0))
        self.assertIsNone(self.left(DeviceRule(mac="m", win_enabled=True, win_start=300, win_end=300), 12, 0))

    def test_quota_in_seconds_of_use(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=30, used_sec=600)
        self.assertEqual(self.left(d, 12, 0), (1200, "quota"))
        d.used_sec = 9999
        self.assertEqual(self.left(d, 12, 0), (0, "quota"))

    def test_window_by_clock(self):
        d = DeviceRule(mac="m", win_enabled=True, win_start=6 * 60, win_end=21 * 60)
        self.assertEqual(self.left(d, 20, 50, 30), (570, "window"))
        self.assertEqual(self.left(d, 22, 0), (0, "window"))
        d.win_start, d.win_end = 22 * 60, 2 * 60  # across midnight
        self.assertEqual(self.left(d, 23, 0), (3 * 3600, "window"))

    def test_earliest_limit_wins(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=60, win_enabled=True, win_start=6 * 60, win_end=21 * 60)
        self.assertEqual(self.left(d, 20, 40), (1200, "window"))
        self.assertEqual(self.left(d, 12, 0), (3600, "quota"))

    def test_extension_pushes_earlier_limits_to_its_end(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=60, used_sec=3600, extend_min=22 * 60, extend_day=self.DAY)
        self.assertEqual(self.left(d, 21, 30), (1800, "extension"))
        # Quota far from used up: the extension does not shorten it.
        d.used_sec = 0
        self.assertEqual(self.left(d, 21, 30), (3600, "quota"))
        # Window past its end at the extension's end: cut at the extension's end.
        d.quota_enabled, d.win_enabled, d.win_start, d.win_end = False, True, 6 * 60, 21 * 60
        self.assertEqual(self.left(d, 21, 30), (1800, "extension"))

    def test_extension_into_small_hours(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=1, used_sec=60, extend_min=60, extend_day=self.DAY)
        self.assertEqual(self.left(d, 23, 30), (5400, "extension"))

    def test_without_clock_only_quota(self):
        d = DeviceRule(mac="m", quota_enabled=True, quota_min=10, win_enabled=True, win_start=6 * 60, win_end=7 * 60)
        r = time_left(d, datetime(2026, 7, 17, 12, 0), time_valid=False, today=self.DAY, reset_min=RESET)
        self.assertEqual(r, (600, "quota"))


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
