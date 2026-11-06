import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from usage import ledger, report
from usage.attribute import source_of
from usage.pricing import cost

HOME = str(Path.home())


def line(rid, ts="2026-10-29T10:00:00.000Z", cwd=f"{HOME}/dev/proj", ep="cli", model="claude-opus-5-5",
         session="s1", **usage):
    u = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 1000,
         "cache_creation_input_tokens": 300, "cache_creation": {"ephemeral_1h_input_tokens": 200}}
    u.update(usage)
    return json.dumps({"type": "assistant", "requestId": rid, "timestamp": ts, "sessionId": session,
                       "cwd": cwd, "entrypoint": ep, "message": {"model": model, "usage": u}})


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.projects = root / "projects"
        self.state = root / "state"
        self.projects.mkdir()
        self.env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state),
                                                "USAGE_PROJECTS_DIR": str(self.projects)})
        self.env.start()
        self.conn = ledger.connect(self.state / "t.db")

    def tearDown(self):
        self.env.stop()
        self.conn.close()
        self.tmp.cleanup()

    def write(self, rel, *lines, mode="a"):
        p = self.projects / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open(mode) as fh:
            fh.write("".join(x + "\n" for x in lines))
        return p

    def test_streaming_duplicates_count_once_and_synthetic_is_skipped(self):
        self.write("a/s1.jsonl", line("r1"), line("r1"), line("r1"), line("r2"),
                   line("r3", model="<synthetic>"), '{"type": "user", "message": "usage"}')
        self.assertEqual(ledger.scan(self.conn), 2)
        row = self.conn.execute("SELECT cache_5m, cache_1h FROM calls WHERE request_id='r1'").fetchone()
        self.assertEqual(row, (100, 200))  # 300 writes, 200 of them 1-hour

    def test_subagent_files_are_scanned(self):
        self.write("a/s1.jsonl", line("r1"))
        self.write("a/s1/subagents/agent-x.jsonl", line("r2", session="s1"))
        self.assertEqual(ledger.scan(self.conn), 2)

    def test_incremental_and_partial_lines(self):
        p = self.write("a/s1.jsonl", line("r1"))
        self.assertEqual(ledger.scan(self.conn), 1)
        with p.open("a") as fh:
            fh.write(line("r2") + "\n" + line("r3")[:40])  # r3 still being written
        self.assertEqual(ledger.scan(self.conn), 1)
        with p.open("a") as fh:
            fh.write(line("r3")[40:] + "\n")
        self.assertEqual(ledger.scan(self.conn), 1)
        self.assertEqual(ledger.scan(self.conn), 0)

    def test_replaced_file_is_rescanned_without_duplicates(self):
        p = self.write("a/s1.jsonl", line("r1"), line("r2"))
        ledger.scan(self.conn)
        p.unlink()
        self.write("a/s1.jsonl", line("r1"), line("r2"), line("r4"))
        self.assertEqual(ledger.scan(self.conn), 1)

    def test_events_and_tags(self):
        self.state.mkdir(exist_ok=True)
        (self.state / "usage").mkdir(exist_ok=True)
        (self.state / "usage" / "events.jsonl").write_text(json.dumps({
            "request_id": "e1", "ts": "2026-10-29T11:00:00Z", "source": "benchmark:example",
            "model": "claude-sonnet-5-5", "usage": {"input_tokens": 5, "output_tokens": 7}}) + "\nnot json\n")
        (self.state / "usage" / "tags.jsonl").write_text(
            json.dumps({"session_id": "cronS", "source": "cron:daily"}) + "\n")
        self.write("c/x.jsonl", line("r9", cwd=f"{HOME}/dev/cron-jobs", ep="sdk-cli", session="cronS"),
                   line("r10", cwd=f"{HOME}/dev/cron-jobs", ep="sdk-cli", session="other"))
        ledger.scan(self.conn)
        calls = report.load(self.conn, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(sorted(c.source for c in calls),
                         ["benchmark:example", "cron:daily", "cron:untagged"])

    def test_report_renders(self):
        self.write("a/s1.jsonl", line("r1"), line("r2", cwd=f"{HOME}/.cache/pr-review/example-web", ep="sdk-cli"),
                   line("r3", model="claude-unknown-9"))
        ledger.scan(self.conn)
        since = datetime(2026, 1, 1, tzinfo=UTC)
        md = report.render(report.load(self.conn, since), since)
        for heading in ("## By category", "## By source", "## By model", "## Heaviest 5-hour windows", "## By day"):
            self.assertIn(heading, md)
        self.assertIn("review:example-web", md)
        self.assertIn("1 calls use models missing from prices.toml", md)


class AttributionTest(unittest.TestCase):
    def test_rules(self):
        cases = [
            (f"{HOME}/.cache/pr-review/example-web", "sdk-cli", "review:example-web"),
            (f"{HOME}/.cache/pr-review/cron-jobs/daily", "sdk-cli", "review:cron-jobs"),
            (f"{HOME}/.cache/sweep/example-web/extension", "sdk-cli", "sweep:example-web"),
            (f"{HOME}/.cache/sweep-fix/example-web-50", "sdk-cli", "sweep-fix:example-web"),
            (f"{HOME}/.local/share/agent-trader/arms/taker", "sdk-cli", "agent:agent-trader"),
            ("/tmp/bench-abc", "sdk-cli", "benchmark:example"),
            (f"{HOME}/dev/cron-jobs/daily", "sdk-cli", "cron:untagged"),
            (f"{HOME}/dev/cron-jobs", "cli", "interactive:cron-jobs"),
            (f"{HOME}/dev/example-daemon/.claude/worktrees/a1", "cli", "interactive:example-daemon"),
            (f"{HOME}/dev/example-web", "sdk-cli", "headless:example-web"),
            (f"{HOME}/dev", "cli", "interactive:~/dev"),
            (f"{HOME}/notes", "cli", "interactive:~/notes"),
            ("/tmp/elsewhere", "sdk-cli", "other:/tmp/elsewhere"),
        ]
        for cwd, ep, want in cases:
            self.assertEqual(source_of(cwd, ep), want, cwd)
        self.assertEqual(source_of(f"{HOME}/dev/x", "cli", tag="cron:job"), "cron:job")


class PricingAndWindowsTest(unittest.TestCase):
    def test_cost(self):
        # Opus 5.5: 10 in, 20 out, 1000 cache reads, 100 5m writes, 200 1h writes
        want = (10 * 4 + 20 * 20 + 1000 * 0.2 + 100 * 5 + 200 * 8) / 1e6
        self.assertAlmostEqual(cost("claude-opus-5-5", 10, 20, 1000, 100, 200), want)
        self.assertIsNone(cost("claude-unknown-9", 1, 1, 0, 0, 0))
        short = cost("claude-haiku-5-5", 1000, 0, 0, 0, 0)
        self.assertAlmostEqual(cost("claude-haiku-5-5", 200_000, 0, 0, 0, 0), short * 200 * 5)

    def test_windows_open_at_first_call_after_close(self):
        t0 = datetime(2026, 10, 29, 8, tzinfo=UTC)
        mk = lambda h: report.Call(t0 + timedelta(hours=h), "s", "x", "m", 1, 1, 0.0)  # noqa: E731
        wins = report.windows([mk(0), mk(4.9), mk(5.0), mk(9.9), mk(11)])
        self.assertEqual([len(cs) for _, cs in wins], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()
