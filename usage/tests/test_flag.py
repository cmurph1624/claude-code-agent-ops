import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "claude/hooks/usage-flag.py"


class FlagHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "usage"
        self.state.mkdir()
        self.env = {**os.environ, "XDG_STATE_HOME": self.tmp.name}

    def tearDown(self):
        self.tmp.cleanup()

    def limits(self, five=10.0, week=80.0):
        now = time.time()
        (self.state / "limits.json").write_text(json.dumps({
            "five_hour_pct": five, "five_hour_resets_at": now + 3600, "seven_day_pct": week,
            "seven_day_resets_at": now + 86400, "read_at": now, "source": "statusline"}))

    def hook(self, event, **extra):
        p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(
            {"hook_event_name": event, "session_id": "s1", **extra}), capture_output=True, text=True,
            env=self.env, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def test_prompt_warning_once_per_session(self):
        self.limits(week=80)
        out = self.hook("UserPromptSubmit", prompt="hi")
        self.assertIn("weekly limit at 80%", out["systemMessage"])
        self.assertIn("usage override", out["systemMessage"])
        self.assertIn("Avoid token-heavy work", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(self.hook("UserPromptSubmit", prompt="again"))

    def test_heavy_actions_ask_light_ones_pass(self):
        self.limits(five=90)
        for tool, cmd in (("Agent", None), ("Workflow", None), ("Bash", "git push -u origin x"),
                          ("Bash", "claude -p < prompt"),
                          ("Bash", "gh pr create --title t")):
            out = self.hook("PreToolUse", tool_name=tool, tool_input={"command": cmd} if cmd else {})
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask", (tool, cmd))
        for tool, cmd in (("Bash", "ls -la"), ("Bash", "git status"), ("Read", None)):
            self.assertIsNone(self.hook("PreToolUse", tool_name=tool,
                                        tool_input={"command": cmd} if cmd else {}), (tool, cmd))

    def test_quiet_below_threshold_with_override_or_without_reading(self):
        self.assertIsNone(self.hook("UserPromptSubmit"))  # no reading: fail open
        self.limits(five=50, week=60)
        self.assertIsNone(self.hook("PreToolUse", tool_name="Agent", tool_input={}))
        self.limits(week=95)
        (self.state / "override.json").write_text(json.dumps({"until": time.time() + 600}))
        self.assertIsNone(self.hook("PreToolUse", tool_name="Agent", tool_input={}))
        self.assertIsNone(self.hook("UserPromptSubmit"))

    def test_garbage_input_fails_open(self):
        p = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True,
                           text=True, env=self.env, timeout=20)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


class HeadlessGateTest(unittest.TestCase):
    def test_claude_json_refuses_while_paused(self):
        sys.path.insert(0, str(ROOT / "review"))
        import common
        with mock.patch("usage.guard.paused", return_value="weekly limit at 80%"), \
                mock.patch.object(common, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "usage limit: governor paused"):
                common.claude_json("p", ".", {}, "Read", ["Read"])
            run.assert_not_called()

    def test_reviewer_pause_is_logged_once(self):
        sys.path.insert(0, str(ROOT / "review"))
        import review_poller as rp
        with tempfile.TemporaryDirectory() as d, mock.patch.object(rp, "STATE", Path(d)):
            with mock.patch("usage.guard.paused", return_value="5-hour limit at 76%"):
                self.assertTrue(rp.governor_paused())
                self.assertTrue(rp.governor_paused())
            with mock.patch("usage.guard.paused", return_value=None):
                self.assertFalse(rp.governor_paused())
            lines = (Path(d) / "log").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("pausing reviews", lines[0])
            self.assertIn("resumed", lines[1])


if __name__ == "__main__":
    unittest.main()
