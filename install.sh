#!/usr/bin/env bash
# Wire agent-ops into Claude Code and systemd. Idempotent; re-run after pulling changes to units/hooks.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Claude Code: skills, agent, global instructions (an @import, so edits here are live).
mkdir -p ~/.claude/skills ~/.claude/agents
for s in pr-review-loop deploy; do ln -sfn "$here/claude/skills/$s" ~/.claude/skills/$s; done
ln -sf "$here/claude/agents/ops.md" ~/.claude/agents/ops.md
imp="@$here/claude/CLAUDE.global.md"
touch ~/.claude/CLAUDE.md
grep -qxF "$imp" ~/.claude/CLAUDE.md || printf '%s\n' "$imp" >> ~/.claude/CLAUDE.md

# Hooks, merged into settings.json (existing hooks kept):
#   PreToolUse  Bash / mcp__supabase.* / Artifact -> deploy-gate.py  (deploys always prompt)
#   PostToolUse Bash                              -> on-pr-push.py   (nudge the review loop after a PR push)
python3 - "$here/claude/hooks" <<'PY'
import json, shutil, sys, time
from pathlib import Path
p = Path.home() / ".claude/settings.json"
s = json.loads(p.read_text()) if p.exists() else {}
changed = False
for event, matcher, script, timeout in (("PreToolUse", "Bash", "deploy-gate.py", 10),
                                       ("PreToolUse", "mcp__supabase.*", "deploy-gate.py", 10),
                                       ("PreToolUse", "Artifact", "deploy-gate.py", 10),
                                       ("PostToolUse", "Bash", "on-pr-push.py", 20)):
    cmd = f"python3 {sys.argv[1]}/{script}"
    lst = s.setdefault("hooks", {}).setdefault(event, [])
    if not any(e.get("matcher") == matcher and h.get("command") == cmd for e in lst for h in e.get("hooks", [])):
        lst.append({"matcher": matcher, "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
        changed = True
        print(f"{event} {matcher} hook added: {script}")
if changed:
    shutil.copy(p, p.with_name(f"settings.json.backup-agent-ops-{int(time.time())}"))
    p.write_text(json.dumps(s, indent=2) + "\n")
PY

# Reviewer timer.
mkdir -p ~/.config/systemd/user
ln -sf "$here/systemd/pr-review.service" ~/.config/systemd/user/pr-review.service
ln -sf "$here/systemd/pr-review.timer" ~/.config/systemd/user/pr-review.timer
systemctl --user daemon-reload
systemctl --user enable --now pr-review.timer
systemctl --user list-timers pr-review.timer --no-pager
