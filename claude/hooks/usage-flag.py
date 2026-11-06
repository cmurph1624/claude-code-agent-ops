#!/usr/bin/env python3
"""Usage governor hook, for every Claude Code session.

UserPromptSubmit: when the 5-hour or weekly limit is at 75%+ (gate `flag`), warn once per session
  per window. The user sees a system message, and Claude is told to hold off heavy work.
PreToolUse (Bash / Agent / Task / Workflow): while flagged, token-heavy actions ask for approval:
  spawning subagents or workflows, `claude -p`, `git push` and `gh pr create`
  (each push to a watched repo triggers a PR review).

`usage override 2h` (or 30m / until-reset) lifts both. Reads the gate that the status line and
usage-poll.timer keep current. Fails open (no output) on any error.
"""
import json
import re
import sys
from pathlib import Path

HEAVY_TOOLS = {"Agent", "Task", "Workflow"}
# Add your own token-heavy commands (benchmark runners, batch jobs) to this pattern.
HEAVY_BASH = re.compile(r"\bclaude\s+(-p|--print)\b|\bgit\s+push\b|\bgh\s+pr\s+create\b")


def main():
    ev = json.load(sys.stdin)
    event = ev.get("hook_event_name")
    if event == "PreToolUse":
        tool = ev.get("tool_name", "")
        cmd = (ev.get("tool_input") or {}).get("command", "") if tool == "Bash" else ""
        if tool not in HEAVY_TOOLS and not HEAVY_BASH.search(cmd):
            return
    elif event != "UserPromptSubmit":
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from usage import gate  # deferred: only on a heavy action or a prompt
    from usage.paths import state_dir

    g = gate.refresh()
    if g.state != "flag":
        return
    hint = "Run `usage override 2h` (or 30m / until-reset) to lift it, `usage report --since 12h` to see what used it."
    if event == "PreToolUse":
        what = ev.get("tool_name") if ev.get("tool_name") != "Bash" else "this command"
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"Usage governor: {g.reason}. {what} spends a lot of tokens. {hint}"}}))
        return
    # UserPromptSubmit: once per session per window.
    key = f"{g.limit}@{int(g.resets_at or 0)}"
    seen_path = state_dir() / "warned.json"
    try:
        seen = json.loads(seen_path.read_text())
    except (OSError, ValueError):
        seen = {}
    sid = ev.get("session_id") or "?"
    if seen.get(sid) == key:
        return
    seen[sid] = key
    seen = dict(list(seen.items())[-200:])
    seen_path.write_text(json.dumps(seen))
    print(json.dumps({
        "systemMessage": f"⚠ Usage governor: {g.reason}. Heavy work (subagents, workflows, claude -p, "
                         f"benchmark runs, PR pushes) now asks for approval. {hint}",
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
            f"The user's Claude subscription usage is high: {g.reason}. Paused automatically: PR reviews, "
            "sweeps, benchmark runs, agents. Avoid token-heavy work (subagents, workflows, long "
            "explorations, large reads) unless the user asks for it; prefer small, direct steps, and "
            "mention the limit when proposing anything expensive.")},
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        sys.exit(0)
