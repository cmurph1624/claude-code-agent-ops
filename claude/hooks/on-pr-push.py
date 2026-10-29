#!/usr/bin/env python3
"""PostToolUse hook (Bash): after `gh pr create`, or a `git push` to a branch with an open PR in a
watched repo, tell the session to run the pr-review-loop skill. It stays quiet otherwise, and
fails open: any error means no nudge, never a blocked tool call."""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review"))

try:
    ev = json.load(sys.stdin)
    cmd = ev.get("tool_input", {}).get("command", "")
    if not re.search(r"\bgh\s+pr\s+create\b|\bgit\s+push\b", cmd):
        sys.exit(0)
    from common import slug_for_dir  # noqa: E402  (deferred: only pay for it on a match)
    cwd = ev.get("cwd") or "."
    slug = slug_for_dir(cwd)
    if not slug:
        sys.exit(0)
    p = subprocess.run(["gh", "pr", "view", "--json", "number,state,isDraft"], cwd=cwd,
                       capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        sys.exit(0)
    pr = json.loads(p.stdout)
    if pr["state"] != "OPEN" or pr["isDraft"]:
        sys.exit(0)
    msg = (f"PR #{pr['number']} on {slug} will be reviewed automatically within a few minutes. "
           f"Run the pr-review-loop skill for PR #{pr['number']} now. It waits for the review and "
           f"applies the findings. If you're already inside that loop, carry on with it.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
except Exception:  # noqa: BLE001
    sys.exit(0)
