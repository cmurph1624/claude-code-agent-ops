import json
import os
import tempfile
import unittest
from unittest import mock

from usage import alerts, limits, statusline
from usage import gate as g
from usage.limits import Reading

NOW = 1_800_000_000.0


def reading(five=10.0, week=40.0, age=0.0, five_reset=NOW + 3600, week_reset=NOW + 3 * 86400):
    return Reading(five, five_reset, week, week_reset, NOW - age, "statusline")


class GateTest(unittest.TestCase):
    def test_ok_and_flag_on_either_limit(self):
        self.assertEqual(g.compute(reading(), None, NOW).state, "ok")
        five = g.compute(reading(five=80), None, NOW)
        self.assertEqual((five.state, five.limit, five.pct), ("flag", "5-hour", 80))
        week = g.compute(reading(week=76), None, NOW)
        self.assertEqual((week.state, week.limit), ("flag", "weekly"))
        self.assertEqual(week.resets_at, NOW + 3 * 86400)

    def test_override_lifts_the_flag_until_it_expires(self):
        self.assertEqual(g.compute(reading(week=90), NOW + 60, NOW).state, "ok")
        self.assertEqual(g.compute(reading(week=90), NOW - 1, NOW).state, "flag")

    def test_stale_or_missing_reading_fails_open(self):
        self.assertEqual(g.compute(None, None, NOW).state, "unknown")
        self.assertEqual(g.compute(reading(week=99, age=g.STALE_AFTER + 1), None, NOW).state, "unknown")

    def test_a_window_that_already_reset_counts_as_zero(self):
        self.assertEqual(g.compute(reading(five=95, five_reset=NOW - 1), None, NOW).state, "ok")

    def test_parse_duration(self):
        gate = g.compute(reading(week=80), None, NOW)
        self.assertEqual(g.parse_duration("2h", gate, NOW), NOW + 7200)
        self.assertEqual(g.parse_duration("30m", gate, NOW), NOW + 1800)
        self.assertEqual(g.parse_duration("until-reset", gate, NOW), NOW + 3 * 86400)
        self.assertIsNone(g.parse_duration("off", gate, NOW))
        with self.assertRaises(ValueError):
            g.parse_duration("forever", gate, NOW)


class ReadingsTest(unittest.TestCase):
    def test_statusline_and_endpoint_normalise_the_same(self):
        sl = limits.from_statusline({"rate_limits": {
            "five_hour": {"used_percentage": 12, "resets_at": 1_800_003_600},
            "seven_day": {"used_percentage": 54, "resets_at": 1_800_250_000}}}, NOW)
        ep = limits.from_endpoint({
            "five_hour": {"utilization": 12.0, "resets_at": "2027-01-15T08:20:00+00:00"},
            "seven_day": {"utilization": 54.0, "resets_at": "2027-01-18T05:06:40+00:00"}}, NOW)
        self.assertEqual((sl.five_hour_pct, sl.seven_day_pct), (12, 54))
        self.assertEqual((ep.five_hour_pct, ep.seven_day_pct), (12, 54))
        self.assertEqual(sl.five_hour_resets_at, 1_800_003_600)
        self.assertIsNone(limits.from_statusline({}, NOW))  # before the first API response


class AlertsAndStatuslineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_each_crossing_alerts_once_per_window(self):
        sent: list[str] = []
        send = lambda h, d: sent.append(h) or True  # noqa: E731
        alerts.check(reading(week=80), send)
        alerts.check(reading(week=82), send)
        self.assertEqual(sent, ["Claude weekly limit at 80% (crossed 75%)"])
        alerts.check(reading(week=91), send)
        self.assertEqual(len(sent), 2)
        alerts.check(reading(week=80, week_reset=NOW + 10 * 86400), send)  # a new week
        self.assertEqual(len(sent), 3)

    def test_failed_send_is_retried(self):
        alerts.check(reading(five=76), lambda h, d: False)
        sent: list[str] = []
        alerts.check(reading(five=76), lambda h, d: sent.append(h) or True)
        self.assertEqual(len(sent), 1)

    def test_override_and_gate_file(self):
        limits.save(reading(week=80, age=0))
        with mock.patch("time.time", return_value=NOW):
            self.assertEqual(g.refresh().state, "flag")
            g.set_override(NOW + 600)
            self.assertEqual(g.refresh().state, "ok")
            self.assertEqual(json.loads(open(os.path.join(self.tmp.name, "usage", "gate.json")).read())["state"], "ok")
            g.set_override(None)
            self.assertEqual(g.refresh().state, "flag")

    def test_statusline_render(self):
        r = reading(five=12, week=80)
        line = statusline.render({"workspace": {"current_dir": "/x/example-web"},
                                  "model": {"display_name": "Opus 5.5"}},
                                 r, g.compute(r, None, NOW), NOW)
        self.assertIn("example-web", line)
        self.assertIn("5h 12%", line)
        self.assertIn("80%", line)
        self.assertIn("usage override 2h", line)


if __name__ == "__main__":
    unittest.main()
