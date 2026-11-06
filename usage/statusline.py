"""Claude Code status line: show the limits, and save the reading for the gate.

Configured as {"statusLine": {"type": "command", "command": "~/.local/bin/usage statusline"}}.
Claude Code pipes the session JSON on stdin after each response; we print one line.
"""

import json
import os
import sys
import time

from usage import gate as gating
from usage import limits

RESET, DIM = "\033[0m", "\033[2m"
COLOURS = ((90, "\033[31m"), (75, "\033[33m"), (50, "\033[36m"))


def _c(pct: float | None) -> str:
    if pct is None:
        return "–"
    colour = next((c for level, c in COLOURS if pct >= level), "")
    return f"{colour}{pct:.0f}%{RESET if colour else ''}"


def _when(ts: float | None, now: float) -> str:
    if not ts:
        return ""
    fmt = "%H:%M" if ts - now < 20 * 3600 else "%a %H:%M"
    return time.strftime(fmt, time.localtime(ts))


def render(data: dict, r: limits.Reading | None, g: gating.Gate, now: float) -> str:
    parts = []
    cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or ""
    if cwd:
        parts.append(os.path.basename(cwd.rstrip("/")) or cwd)
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(model)
    if r is None:
        parts.append(f"{DIM}limits: no reading yet{RESET}")
    else:
        five = f"5h {_c(r.five_hour_pct)}"
        if r.five_hour_resets_at:
            five += f" {DIM}→{_when(r.five_hour_resets_at, now)}{RESET}"
        week = f"wk {_c(r.seven_day_pct)}"
        if r.seven_day_resets_at:
            week += f" {DIM}→{_when(r.seven_day_resets_at, now)}{RESET}"
        parts += [five, week]
        if now - r.read_at > gating.STALE_AFTER:
            parts.append(f"{DIM}(stale){RESET}")
    if g.state == "flag":
        parts.append("\033[1;33m⚠ heavy work needs approval · usage override 2h\033[0m")
    elif g.override_until:
        parts.append(f"{DIM}override →{_when(g.override_until, now)}{RESET}")
    return " · ".join(parts)


def main() -> int:
    now = time.time()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    fresh = limits.from_statusline(data, now)
    if fresh is not None:
        try:
            limits.save(fresh)
        except OSError:
            pass
    r = fresh or limits.load()
    try:
        g = gating.refresh()
    except OSError:
        g = gating.compute(r, gating.load_override(), now)
    print(render(data, r, g, now))
    return 0
