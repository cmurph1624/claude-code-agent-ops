"""SQLite ledger and the incremental scanner that fills it."""

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from usage.paths import projects_dir, state_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    request_id   TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,          -- ISO 8601 UTC
    session_id   TEXT,
    cwd          TEXT,
    entrypoint   TEXT,                   -- cli | sdk-cli | event
    sidechain    INTEGER NOT NULL DEFAULT 0,
    model        TEXT,
    input        INTEGER NOT NULL DEFAULT 0,
    output       INTEGER NOT NULL DEFAULT 0,
    cache_read   INTEGER NOT NULL DEFAULT 0,
    cache_5m     INTEGER NOT NULL DEFAULT 0,
    cache_1h     INTEGER NOT NULL DEFAULT 0,
    source_tag   TEXT                    -- set for events.jsonl records
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE TABLE IF NOT EXISTS tags (session_id TEXT PRIMARY KEY, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offsets (path TEXT PRIMARY KEY, inode INTEGER, pos INTEGER);
CREATE TABLE IF NOT EXISTS limits (
    ts TEXT NOT NULL, five_hour_pct REAL, five_hour_resets_at INTEGER,
    seven_day_pct REAL, seven_day_resets_at INTEGER, reading_source TEXT
);
CREATE TABLE IF NOT EXISTS events (ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""

INSERT = """
INSERT OR IGNORE INTO calls (request_id, ts, session_id, cwd, entrypoint, sidechain, model,
    input, output, cache_read, cache_5m, cache_1h, source_tag)
VALUES (:request_id, :ts, :session_id, :cwd, :entrypoint, :sidechain, :model,
    :input, :output, :cache_read, :cache_5m, :cache_1h, :source_tag)
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or state_dir() / "usage.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def _new_lines(conn: sqlite3.Connection, path: Path) -> Iterator[str]:
    """Complete lines appended since the last scan; restarts if the file was replaced."""
    st = path.stat()
    row = conn.execute("SELECT inode, pos FROM offsets WHERE path = ?", (str(path),)).fetchone()
    pos = row[1] if row and row[0] == st.st_ino and row[1] <= st.st_size else 0
    if pos == st.st_size:
        return
    with path.open("rb") as fh:
        fh.seek(pos)
        data = fh.read()
    end = data.rfind(b"\n") + 1  # leave a partly written last line for next time
    for raw in data[:end].splitlines():
        yield raw.decode("utf-8", errors="replace")
    conn.execute(
        "INSERT OR REPLACE INTO offsets (path, inode, pos) VALUES (?, ?, ?)",
        (str(path), st.st_ino, pos + end),
    )


def transcript_call(line: str) -> dict | None:
    """The call record in one transcript line, or None if the line isn't an API response."""
    if '"usage"' not in line or '"assistant"' not in line:
        return None
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return None
    msg = d.get("message") or {}
    u = msg.get("usage")
    if d.get("type") != "assistant" or not u or msg.get("model") == "<synthetic>":
        return None
    rid = d.get("requestId") or msg.get("id")
    if not rid:
        return None
    writes_1h = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    writes = int(u.get("cache_creation_input_tokens") or 0)
    return {
        "request_id": rid,
        "ts": d.get("timestamp") or "",
        "session_id": d.get("sessionId"),
        "cwd": d.get("cwd"),
        "entrypoint": d.get("entrypoint"),
        "sidechain": int(bool(d.get("isSidechain"))),
        "model": msg.get("model"),
        "input": int(u.get("input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_5m": max(0, writes - writes_1h),
        "cache_1h": writes_1h,
        "source_tag": None,
    }


def event_call(line: str) -> dict | None:
    """A record from events.jsonl: {request_id, ts, source, model, usage{...}}."""
    try:
        d = json.loads(line)
        u = d.get("usage") or {}
        return {
            "request_id": str(d["request_id"]),
            "ts": str(d["ts"]),
            "session_id": d.get("session_id"),
            "cwd": d.get("cwd"),
            "entrypoint": "event",
            "sidechain": 0,
            "model": d.get("model"),
            "input": int(u.get("input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
            "cache_read": int(u.get("cache_read_tokens") or 0),
            "cache_5m": int(u.get("cache_write_tokens") or 0),
            "cache_1h": int(u.get("cache_write_1h_tokens") or 0),
            "source_tag": str(d["source"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def scan(conn: sqlite3.Connection, root: Path | None = None) -> int:
    """Read new transcript, event and tag lines into the ledger. Returns calls added."""
    before = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    for dirpath, _dirs, files in os.walk(root or projects_dir()):
        for name in files:
            if name.endswith(".jsonl"):
                path = Path(dirpath) / name
                rows = [c for c in map(transcript_call, _new_lines(conn, path)) if c]
                conn.executemany(INSERT, rows)
    state = state_dir()
    events = state / "events.jsonl"
    if events.exists():
        conn.executemany(INSERT, [c for c in map(event_call, _new_lines(conn, events)) if c])
    tags = state / "tags.jsonl"
    if tags.exists():
        for line in _new_lines(conn, tags):
            try:
                t = json.loads(line)
                conn.execute(
                    "INSERT OR REPLACE INTO tags (session_id, source) VALUES (?, ?)",
                    (str(t["session_id"]), str(t["source"])),
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    conn.commit()
    after: int = conn.execute("SELECT count(*) FROM calls").fetchone()[0]
    return after - before
