"""Claude usage across every project on this box.

  usage report [--since 7d] [--top 15] [--out FILE]   who spent the tokens (scans first)
  usage scan                                           update the ledger only
  usage status                                         current limits and gate
  usage override [2h|30m|until-reset|off]              let gated work run past 75%
  usage gate [--source NAME]                           exit 0 = go, 3 = paused (for scripts)
  usage poll                                           timer job: scan, read limits, gate, alerts
  usage statusline                                     Claude Code status line (stdin JSON)
"""

import argparse
import sys
import time
from dataclasses import asdict

from usage import alerts, ledger, limits, report
from usage import gate as gating
from usage.paths import state_dir

ENDPOINT_AFTER = 10 * 60  # use the status line's reading while it's this fresh


def _log(msg: str) -> None:
    with (state_dir() / "poll.log").open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")


def poll() -> int:
    conn = ledger.connect()
    added = ledger.scan(conn)
    r = limits.load()
    note = ""
    if r is None or time.time() - r.read_at > ENDPOINT_AFTER:
        try:
            r = limits.fetch_endpoint()
            limits.save(r)
            note = "endpoint"
        except Exception as e:  # noqa: BLE001  fail open: keep the old reading, gate goes stale
            note = f"endpoint failed: {type(e).__name__}: {e}"[:200]
    else:
        note = r.source
    if r is not None:
        conn.execute(
            "INSERT INTO limits (ts, five_hour_pct, five_hour_resets_at, seven_day_pct,"
            " seven_day_resets_at, reading_source) VALUES (?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.read_at)), r.five_hour_pct,
             r.five_hour_resets_at, r.seven_day_pct, r.seven_day_resets_at, r.source))
        conn.commit()
    g = gating.refresh()
    sent = alerts.check(r)
    _log(f"+{added} calls; reading {note}; gate {g.state}: {g.reason}"
         + (f"; slack: {'; '.join(sent)}" if sent else ""))
    return 0


def status() -> int:
    r = limits.load()
    g = gating.refresh()
    now = time.time()
    if r is None:
        print("No limits reading yet (run `usage poll`, or open a Claude session).")
    else:
        age = int((now - r.read_at) / 60)
        for name, pct, resets in (("5-hour", r.five_hour_pct, r.five_hour_resets_at),
                                  ("weekly", r.seven_day_pct, r.seven_day_resets_at)):
            when = time.strftime("%a %H:%M", time.localtime(resets)) if resets else "?"
            print(f"{name:7} {'–' if pct is None else f'{pct:.0f}%':>5}  resets {when}")
        print(f"(read {age} min ago from the {r.source})")
    print(f"gate: {g.state} — {g.reason}")
    return 0


def override(arg: str) -> int:
    g = gating.refresh()
    try:
        until = gating.parse_duration(arg, g, time.time())
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    gating.set_override(until)
    conn = ledger.connect()
    conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, 'override', ?)",
                 (time.strftime("%Y-%m-%dT%H:%M:%S"), arg))
    conn.commit()
    g = gating.refresh()
    print("override off" if until is None else
          f"override until {time.strftime('%a %H:%M', time.localtime(until))}")
    print(f"gate: {g.state} — {g.reason}")
    return 0


def gate_cmd(source: str) -> int:
    g = gating.refresh()
    if g.state == "flag":
        print(f"paused{f' {source}' if source else ''}: {g.reason}. Override: usage override 2h")
        conn = ledger.connect()
        conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, 'paused', ?)",
                     (time.strftime("%Y-%m-%dT%H:%M:%S"), f"{source}: {g.reason}"))
        conn.commit()
        return 3
    if g.state == "unknown":
        print(f"warning: {g.reason}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="usage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    r = sub.add_parser("report")
    r.add_argument("--since", default="7d", help="7d, 12h or an ISO date (default 7d)")
    r.add_argument("--top", type=int, default=15)
    r.add_argument("--out", default="")
    sub.add_parser("status")
    o = sub.add_parser("override")
    o.add_argument("duration", nargs="?", default="2h", help="30m, 2h, until-reset or off")
    gp = sub.add_parser("gate")
    gp.add_argument("--source", default="")
    sub.add_parser("poll")
    sub.add_parser("statusline")
    sub.add_parser("json", help="print gate.json (debug)")
    a = ap.parse_args(argv)

    if a.cmd == "statusline":
        from usage import statusline
        return statusline.main()
    if a.cmd == "poll":
        return poll()
    if a.cmd == "status":
        return status()
    if a.cmd == "override":
        return override(a.duration)
    if a.cmd == "gate":
        return gate_cmd(a.source)
    if a.cmd == "json":
        print(asdict(gating.refresh()))
        return 0
    conn = ledger.connect()
    added = ledger.scan(conn)
    if a.cmd == "scan":
        print(f"{added:,} new calls")
        return 0
    since = report.since_arg(a.since)
    text = report.render(report.load(conn, since), since, a.top)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
