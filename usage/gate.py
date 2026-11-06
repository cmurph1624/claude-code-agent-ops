"""The gate: one small JSON file every consumer reads (gate.json), no imports needed.

    {"state": "ok" | "flag" | "unknown", "pct": 78.0, "limit": "weekly", "resets_at": ...,
     "override_until": ..., "reason": "...", "computed_at": ...}

flag    = the 5-hour or weekly limit is at or above THRESHOLD and no override is active
unknown = no reading newer than STALE_AFTER: consumers fail open (run, but warn)
"""

import json
import re
import time
from dataclasses import asdict, dataclass

from usage.limits import Reading, _atomic_write
from usage.limits import load as load_reading
from usage.paths import state_dir

THRESHOLD = 75.0
STALE_AFTER = 30 * 60


@dataclass(frozen=True)
class Gate:
    state: str
    pct: float | None
    limit: str | None  # "5-hour" | "weekly"
    resets_at: float | None
    override_until: float | None
    reason: str
    computed_at: float


def _live(pct: float | None, resets_at: float | None, now: float) -> float | None:
    """A limit whose window has already reset counts as 0, whatever the old reading said."""
    if pct is None:
        return None
    return 0.0 if resets_at is not None and resets_at <= now else pct


def compute(r: Reading | None, override_until: float | None, now: float | None = None,
            threshold: float = THRESHOLD) -> Gate:
    now = now or time.time()
    ov = override_until if override_until and override_until > now else None
    if r is None or now - r.read_at > STALE_AFTER:
        age = "no reading yet" if r is None else f"last reading {int((now - r.read_at) / 60)} min old"
        return Gate("unknown", None, None, None, ov, f"usage unknown ({age}); not gating", now)
    limits = [("5-hour", _live(r.five_hour_pct, r.five_hour_resets_at, now), r.five_hour_resets_at),
              ("weekly", _live(r.seven_day_pct, r.seven_day_resets_at, now), r.seven_day_resets_at)]
    known = [x for x in limits if x[1] is not None]
    if not known:
        return Gate("unknown", None, None, None, ov, "reading has no limits; not gating", now)
    name, pct, resets = max(known, key=lambda x: x[1])  # type: ignore[arg-type, return-value]
    assert pct is not None
    if pct < threshold:
        return Gate("ok", pct, name, resets, ov, f"{name} limit at {pct:.0f}%", now)
    if ov:
        return Gate("ok", pct, name, resets, ov,
                    f"{name} limit at {pct:.0f}%, override until {time.strftime('%H:%M', time.localtime(ov))}", now)
    return Gate("flag", pct, name, resets, None, f"{name} limit at {pct:.0f}% (≥{threshold:.0f}%)", now)


# --- override -------------------------------------------------------------------------------

def load_override() -> float | None:
    try:
        return float(json.loads((state_dir() / "override.json").read_text())["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def parse_duration(text: str, gate: Gate | None, now: float) -> float | None:
    """'2h', '30m', 'until-reset' -> unix time; 'off' -> None."""
    if text == "off":
        return None
    if text == "until-reset":
        if gate is None or gate.resets_at is None:
            raise ValueError("no reset time known yet; use a duration like 2h")
        return gate.resets_at
    m = re.fullmatch(r"(\d+)([mh])", text)
    if not m:
        raise ValueError("use 30m, 2h, until-reset or off")
    return now + int(m.group(1)) * (60 if m.group(2) == "m" else 3600)


def set_override(until: float | None) -> None:
    p = state_dir() / "override.json"
    if until is None:
        p.unlink(missing_ok=True)
    else:
        _atomic_write(p, {"until": until, "set_at": time.time()})


# --- gate.json ------------------------------------------------------------------------------

def refresh() -> Gate:
    g = compute(load_reading(), load_override())
    _atomic_write(state_dir() / "gate.json", asdict(g))
    return g


def read_gate_file() -> dict | None:
    """What consumers do: read gate.json, treat anything unreadable as unknown."""
    try:
        return json.loads((state_dir() / "gate.json").read_text())
    except (OSError, ValueError):
        return None
