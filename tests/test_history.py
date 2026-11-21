"""SolarHistory: daily records, rollover, publishing."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from victronble import history  # noqa: E402

V = {"/Yield/Power": 100, "/Dc/0/Voltage": 13.5, "/Dc/0/Current": 6.5, "/ErrorCode": 0, "/State": 3}


class SolarHistoryTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(len(history.PATHS), 1 + history.DAYS * len(history.DAY_PATHS))
        h = history.SolarHistory()
        values = h.values()
        self.assertEqual(set(values), set(history.PATHS))
        self.assertEqual(values["/History/Overall/DaysAvailable"], 0)
        self.assertIsNone(values["/History/Daily/0/Yield"])

    def test_day(self):
        h = history.SolarHistory()
        h.sample(V, "2026-11-15")
        h.sample(dict(V, **{"/Yield/Power": 150, "/Dc/0/Voltage": 13.1, "/Dc/0/Current": 9.0}), "2026-11-15")
        h.add_energy(0.25, "2026-11-15")
        h.add_energy(0.1, "2026-11-15")
        h.add_energy(None, "2026-11-15")
        v = h.values()
        d = "/History/Daily/0/"
        self.assertEqual(v["/History/Overall/DaysAvailable"], 1)
        self.assertEqual(v[d + "Yield"], 0.35)
        self.assertEqual(v[d + "MaxPower"], 150)
        self.assertEqual((v[d + "MinBatteryVoltage"], v[d + "MaxBatteryVoltage"]), (13.1, 13.5))
        self.assertEqual(v[d + "MaxBatteryCurrent"], 9.0)
        self.assertEqual(v[d + "LastError1"], 0)
        self.assertEqual(h.changes(), {})

    def test_errors_most_recent_first(self):
        h = history.SolarHistory()
        for e in (0, 2, 2, 17, 0, 2, 33, 38):
            h.sample(dict(V, **{"/ErrorCode": e}), "2026-11-15")
        v = h.values()
        self.assertEqual([v["/History/Daily/0/LastError%d" % i] for i in (1, 2, 3, 4)], [38, 33, 2, 17])

    def test_time_in_states(self):
        h = history.SolarHistory()
        t = 100.0
        for state, seconds in ((3, 600), (4, 1800), (5, 300), (0, 600), (6, 60)):
            for _ in range(int(seconds / 5)):
                h.account_time(state, t, "2026-11-15")
                t += 5
        h.pause()
        h.account_time(5, t + 3600, "2026-11-15")  # after a pause: nothing counted
        h.account_time(5, t + 3600 + 1000, "2026-11-15")  # a long step counts MAX_GAP (1 min)
        v = h.values()
        d = "/History/Daily/0/"
        # each state change counts the step before it to the state of that sample
        self.assertEqual(v[d + "TimeInBulk"], 9)
        self.assertEqual(v[d + "TimeInAbsorption"], 30)
        self.assertEqual(v[d + "TimeInFloat"], 7)

    def test_rollover_and_gaps(self):
        h = history.SolarHistory()
        h.add_energy(0.5, "2026-11-10")
        h.add_energy(0.3, "2026-11-11")
        h.today("2026-11-14")
        self.assertEqual(
            [d["date"] for d in h.days],
            ["2026-11-14", "2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10"],
        )
        v = h.values()
        self.assertEqual(v["/History/Overall/DaysAvailable"], 5)
        self.assertEqual(v["/History/Daily/0/Yield"], 0.0)
        self.assertIsNone(v["/History/Daily/1/Yield"])
        self.assertIsNone(v["/History/Daily/2/TimeInBulk"])
        self.assertEqual(v["/History/Daily/3/Yield"], 0.3)
        self.assertEqual(v["/History/Daily/4/Yield"], 0.5)
        # the clock went back (boot before NTP): still day 0
        h.add_energy(0.1, "2026-11-01")
        self.assertEqual(h.days[0]["date"], "2026-11-14")
        self.assertEqual(h.changes()["/History/Daily/0/Yield"], 0.1)
        # only the changed day is republished
        h.add_energy(0.1, "2026-11-14")
        self.assertEqual(set(k.split("/")[3] for k in h.changes() if k.startswith("/History/Daily/")), {"0"})

    def test_capped_at_31_days(self):
        h = history.SolarHistory()
        h.add_energy(1.0, "2026-01-01")
        h.add_energy(1.0, "2026-03-01")  # 59 days later
        self.assertEqual(len(h.days), history.DAYS)
        self.assertEqual(h.days[0]["date"], "2026-03-01")
        self.assertEqual(h.days[-1]["date"], "2026-01-30")
        self.assertTrue(all(d.get("missing") for d in h.days[1:]))

    def test_saved_roundtrip_and_garbage(self):
        h = history.SolarHistory()
        h.add_energy(0.42, "2026-11-15")
        h2 = history.SolarHistory([{"bad": 1}, "x", None] + h.days)
        self.assertEqual(h2.values()["/History/Daily/0/Yield"], 0.42)
        self.assertEqual(len(history.SolarHistory("garbage").days), 0)


if __name__ == "__main__":
    unittest.main()
