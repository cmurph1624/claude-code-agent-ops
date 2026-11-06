import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import is_docs_only  # noqa: E402


class DocsOnlyTest(unittest.TestCase):
    def test_plain_docs_are_skipped(self):
        self.assertTrue(is_docs_only(["README.md", "docs/guide.md"]))
        self.assertTrue(is_docs_only(["docs/diagram.svg", "CHANGELOG", "notes.txt"]))

    def test_code_or_config_means_review(self):
        self.assertFalse(is_docs_only(["README.md", "src/app.py"]))
        self.assertFalse(is_docs_only([".gitignore"]))
        self.assertFalse(is_docs_only([]))

    def test_agent_markdown_is_behaviour(self):
        for p in ["jobs/daily/prompt.md", "review/review-prompt.md", "CLAUDE.md",
                  "claude/skills/deploy/SKILL.md", ".claude/agents/ops.md", "claude/agents/ops.md",
                  "commands/ship.md", "AGENTS.md", "CONTEXT.md"]:
            self.assertFalse(is_docs_only([p]), p)

    def test_docs_outside_top_level_and_docs_dir_are_reviewed(self):
        # e.g. a script that appends a known-issues file to the reviewer's prompt.
        for p in ["deploy/review/known-issues.md", "src/pkg/README.md", "ops/notes.txt"]:
            self.assertFalse(is_docs_only([p]), p)


if __name__ == "__main__":
    unittest.main()
