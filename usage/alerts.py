"""Slack alerts when a limit crosses 75% or 90%, once per limit per window."""

import json
import os
import subprocess
import time
from pathlib import Path

from usage.limits import Reading, _atomic_write
from usage.paths import HOME, state_dir

LEVELS = (75, 90)
# Same optional Slack helper as the PR reviewer: `node <script> <headline> 1` with the detail on
# stdin, and an env file holding SLACK_WEBHOOK_URL. Unset or missing = no alerts.
SLACK = Path(os.environ.get("REVIEW_SLACK_SCRIPT", HOME / "dev/notify/notify-slack.js"))
CRON_ENV = Path(os.environ.get("REVIEW_SLACK_ENV", HOME / "dev/notify/.env"))


def _send(headline: str, detail: str) -> bool:
    """Same webhook helper as the PR reviewer. Best-effort; never raises."""
    if not SLACK.exists():
        return False
    try:
        env = dict(os.environ)
        if CRON_ENV.exists():
            for line in CRON_ENV.read_text().splitlines():
                if line.startswith("SLACK_WEBHOOK_URL="):
                    env["SLACK_WEBHOOK_URL"] = line.split("=", 1)[1].strip().strip("\"'")
        p = subprocess.run(["node", str(SLACK), headline, "1"], input=detail, text=True, env=env,
                           timeout=30, capture_output=True)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def due(r: Reading, sent: dict[str, list[int]]) -> list[tuple[str, int, float, float | None]]:
    """(limit, level, pct, resets_at) crossings not yet alerted in this window."""
    out = []
    for name, pct, resets in (("5-hour", r.five_hour_pct, r.five_hour_resets_at),
                              ("weekly", r.seven_day_pct, r.seven_day_resets_at)):
        if pct is None:
            continue
        key = f"{name}@{int(resets or 0)}"
        for level in LEVELS:
            if pct >= level and level not in sent.get(key, []):
                out.append((name, level, pct, resets))
    return out


def check(r: Reading | None, send=_send) -> list[str]:  # type: ignore[no-untyped-def]
    if r is None:
        return []
    path = state_dir() / "alerts.json"
    try:
        sent: dict[str, list[int]] = json.loads(path.read_text())
    except (OSError, ValueError):
        sent = {}
    messages = []
    for name, level, pct, resets in due(r, sent):
        when = time.strftime("%a %H:%M", time.localtime(resets)) if resets else "unknown"
        headline = f"Claude {name} limit at {pct:.0f}% (crossed {level}%)"
        detail = (f"The {name} limit resets {when}. Gated jobs (PR reviews, sweeps, benchmark "
                  "runs, headless agents) pause at 75%. Run `usage report --since 12h` "
                  "to see what used it, or `usage override 2h` to keep going.")
        if send(headline, detail):
            sent.setdefault(f"{name}@{int(resets or 0)}", []).append(level)
            messages.append(headline)
    # Keep only current windows.
    live = {f"5-hour@{int(r.five_hour_resets_at or 0)}", f"weekly@{int(r.seven_day_resets_at or 0)}"}
    _atomic_write(path, {k: v for k, v in sent.items() if k in live})
    return messages
