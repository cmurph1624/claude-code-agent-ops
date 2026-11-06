"""Markdown usage report: which sources spend the subscription's tokens."""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from usage.attribute import kind, source_of
from usage.pricing import cost, prices

WINDOW = timedelta(hours=5)


@dataclass
class Call:
    ts: datetime
    session: str
    source: str
    model: str
    tokens: int  # prompt + output
    output: int
    usd: float | None


@dataclass
class Bucket:
    calls: int = 0
    tokens: int = 0
    output: int = 0
    usd: float = 0.0
    unpriced: int = 0
    sessions: set[str] = field(default_factory=set)

    def add(self, c: Call) -> None:
        self.calls += 1
        self.tokens += c.tokens
        self.output += c.output
        self.sessions.add(c.session)
        if c.usd is None:
            self.unpriced += 1
        else:
            self.usd += c.usd


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load(conn: sqlite3.Connection, since: datetime) -> list[Call]:
    rows = conn.execute(
        "SELECT c.ts, coalesce(c.session_id, c.request_id), c.cwd, c.entrypoint,"
        " coalesce(c.source_tag, t.source), c.model, c.input, c.output, c.cache_read,"
        " c.cache_5m, c.cache_1h"
        " FROM calls c LEFT JOIN tags t ON t.session_id = c.session_id"
        " WHERE c.ts >= ? ORDER BY c.ts",
        (since.strftime("%Y-%m-%dT%H:%M:%S"),),
    ).fetchall()
    out = []
    for ts, session, cwd, ep, tag, model, i, o, cr, c5, c1 in rows:
        out.append(Call(_parse(ts), session, source_of(cwd, ep, tag), model or "?",
                        i + o + cr + c5 + c1, o, cost(model, i, o, cr, c5, c1)))
    return out


def windows(calls: list[Call]) -> list[tuple[datetime, list[Call]]]:
    """Approximate the subscription's 5-hour windows: each opens at the first call after the
    previous one closed. All sources share one account, so they share the windows."""
    out: list[tuple[datetime, list[Call]]] = []
    for c in calls:
        if not out or c.ts >= out[-1][0] + WINDOW:
            out.append((c.ts, []))
        out[-1][1].append(c)
    return out


def _group(calls: list[Call], key) -> dict[str, Bucket]:  # type: ignore[no-untyped-def]
    g: dict[str, Bucket] = defaultdict(Bucket)
    for c in calls:
        g[key(c)].add(c)
    return dict(sorted(g.items(), key=lambda kv: -kv[1].usd))


def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _m(n: int) -> str:
    return f"{n / 1e6:,.1f}M" if n >= 1e5 else f"{n / 1e3:,.0f}K"


def _table(head: list[str], rows: list[list[str]]) -> str:
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                     + ["| " + " | ".join(r) + " |" for r in rows])


def render(calls: list[Call], since: datetime, top: int = 15) -> str:
    total = Bucket()
    for c in calls:
        total.add(c)
    if not calls:
        return f"# Claude usage since {since:%Y-%m-%d}\n\nNo calls recorded."
    share = (lambda b: f"{100 * b.usd / total.usd:.0f}%") if total.usd else (lambda b: "–")
    out = [
        f"# Claude usage since {since:%Y-%m-%d %H:%M} UTC",
        "",
        f"{total.calls:,} API calls, {_m(total.tokens)} tokens, **{_usd(total.usd)}** "
        f"API-equivalent at list prices (`usage/prices.toml`, as of {prices()['as_of']}). "
        "Subscription limits aren't published in dollars; cost is the common yardstick for "
        "comparing sources, and models weigh differently (Opus costs 2x Sonnet per token).",
    ]
    if total.unpriced:
        out.append(f"\n{total.unpriced} calls use models missing from prices.toml (cost $0).")
    split = _group(calls, lambda c: kind(c.source))
    out += ["", _table(["", "calls", "cost", "share"],
                       [[k, f"{b.calls:,}", _usd(b.usd), share(b)] for k, b in split.items()])]
    cats = _group(calls, lambda c: c.source.split(":", 1)[0])
    out += ["", "## By category", "", _table(
        ["category", "calls", "sources", "cost", "share"],
        [[k, f"{b.calls:,}", str(len({c.source for c in calls if c.source.split(':', 1)[0] == k})),
          _usd(b.usd), share(b)] for k, b in cats.items()],
    )]

    by_source = _group(calls, lambda c: c.source)
    out += ["", "## By source", "", _table(
        ["source", "calls", "sessions", "tokens", "output", "cost", "share"],
        [[s, f"{b.calls:,}", f"{len(b.sessions):,}", _m(b.tokens), _m(b.output), _usd(b.usd),
          share(b)] for s, b in list(by_source.items())[:top]],
    )]
    if len(by_source) > top:
        rest = Bucket()
        for b in list(by_source.values())[top:]:
            rest.calls += b.calls
            rest.usd += b.usd
        out.append(f"\n…and {len(by_source) - top} more sources: {rest.calls:,} calls, "
                   f"{_usd(rest.usd)}.")

    out += ["", "## By model", "", _table(
        ["model", "calls", "tokens", "cost", "share"],
        [[m, f"{b.calls:,}", _m(b.tokens), _usd(b.usd), share(b)]
         for m, b in _group(calls, lambda c: c.model).items()],
    )]

    sessions = _group(calls, lambda c: c.session)
    first = {}
    src = {}
    for c in calls:
        first.setdefault(c.session, c.ts)
        src.setdefault(c.session, c.source)
    out += ["", f"## Top {min(top, 10)} sessions", "", _table(
        ["started (UTC)", "source", "calls", "cost"],
        [[f"{first[s]:%m-%d %H:%M}", src[s], f"{b.calls:,}", _usd(b.usd)]
         for s, b in list(sessions.items())[:min(top, 10)]],
    )]

    wins = windows(calls)
    ranked = sorted(wins, key=lambda w: -sum(c.usd or 0 for c in w[1]))[:min(top, 10)]
    rows = []
    for start, cs in ranked:
        b = _group(cs, lambda c: c.source)
        wtotal = sum(x.usd for x in b.values()) or 1
        lead = ", ".join(f"{s} {100 * x.usd / wtotal:.0f}%" for s, x in list(b.items())[:3])
        rows.append([f"{start:%m-%d %H:%M}", f"{len(cs):,}", _usd(sum(x.usd for x in b.values())),
                     lead])
    out += ["", "## Heaviest 5-hour windows", "",
            "Approximate windows (each opens at the first call after the previous one closes).",
            "", _table(["opened (UTC)", "calls", "cost", "top sources"], rows)]

    days = _group(calls, lambda c: f"{c.ts:%Y-%m-%d}")
    out += ["", "## By day", "", _table(
        ["day", "calls", "cost", "top source"],
        [[d, f"{b.calls:,}", _usd(b.usd),
          next(iter(_group([c for c in calls if f"{c.ts:%Y-%m-%d}" == d],
                           lambda c: c.source)))]
         for d, b in sorted(days.items())],
    ), ""]
    return "\n".join(out)


def since_arg(text: str) -> datetime:
    """'7d', '12h', or an ISO date."""
    now = datetime.now(UTC)
    if text[-1:] in "dh" and text[:-1].isdigit():
        n = int(text[:-1])
        return now - (timedelta(days=n) if text[-1] == "d" else timedelta(hours=n))
    return datetime.fromisoformat(text).replace(tzinfo=UTC)
