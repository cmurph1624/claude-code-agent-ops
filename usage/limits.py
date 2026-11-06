"""Current subscription limits: the 5-hour session and the weekly limit.

Two readers write the same normalized reading to limits.json:
- the status line (free, on every interactive turn): Claude Code passes `rate_limits`
- the poller's fallback: GET /api/oauth/usage with the Claude Code login token (undocumented;
  best-effort). The token is never refreshed here, so an expired one just leaves the reading
  stale, and the gate fails open.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from usage.paths import HOME, state_dir

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = HOME / ".claude/.credentials.json"


@dataclass(frozen=True)
class Reading:
    five_hour_pct: float | None
    five_hour_resets_at: float | None  # unix seconds
    seven_day_pct: float | None
    seven_day_resets_at: float | None
    read_at: float
    source: str  # statusline | endpoint


def _epoch(v: object) -> float | None:
    if v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    return datetime.fromisoformat(str(v)).timestamp()


def _pct(v: object) -> float | None:
    return None if v is None else float(v)  # type: ignore[arg-type]


def from_statusline(data: dict, now: float | None = None) -> Reading | None:
    """From the status-line input JSON's `rate_limits` (absent before the first response)."""
    rl = data.get("rate_limits") or {}
    five, week = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    if not five and not week:
        return None
    return Reading(_pct(five.get("used_percentage")), _epoch(five.get("resets_at")),
                   _pct(week.get("used_percentage")), _epoch(week.get("resets_at")),
                   now or time.time(), "statusline")


def from_endpoint(data: dict, now: float | None = None) -> Reading:
    five, week = data.get("five_hour") or {}, data.get("seven_day") or {}
    return Reading(_pct(five.get("utilization")), _epoch(five.get("resets_at")),
                   _pct(week.get("utilization")), _epoch(week.get("resets_at")),
                   now or time.time(), "endpoint")


def fetch_endpoint(timeout: float = 15) -> Reading:
    cred = json.loads(CREDENTIALS.read_text())["claudeAiOauth"]
    if cred.get("expiresAt", 0) / 1000 < time.time():
        raise RuntimeError("Claude login token has expired; a Claude session will refresh it")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {cred['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "agent-ops-usage/0.2",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return from_endpoint(json.load(r))


def _atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


def save(r: Reading) -> None:
    _atomic_write(state_dir() / "limits.json", asdict(r))


def load() -> Reading | None:
    p = state_dir() / "limits.json"
    try:
        return Reading(**json.loads(p.read_text()))
    except (OSError, ValueError, TypeError):
        return None
