"""Machine-wide Claude usage ledger: who is spending the subscription's tokens.

Sources of truth, all local:
- Claude Code transcripts (~/.claude/projects/**/*.jsonl), one line per API call. Lines repeat
  per call while a response streams, so calls are keyed by requestId.
- events.jsonl: call records from programs that keep no transcript (e.g. a benchmark using
  `claude -p --no-session-persistence`).
- tags.jsonl: {session_id, source} written by headless wrappers that pass `--session-id`, so
  runs sharing a working directory (the cron jobs) can still be told apart.

Attribution and pricing happen at report time, so editing sources.toml or prices.toml never
needs a rescan.
"""
