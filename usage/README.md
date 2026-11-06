# usage: who is spending the Claude subscription

## The governor (gate)
The poller (`usage-poll.timer`, every 5 min) and the status line keep one file current,
`~/.local/state/usage/gate.json`:

```
{"state": "ok" | "flag" | "unknown", "pct": 78, "limit": "5-hour" | "weekly", "resets_at": ...,
 "override_until": ..., "reason": "...", "computed_at": ...}
```

- **`flag`**: the 5-hour **or** weekly limit is at 75% or more, with no override active.
- **`unknown`**: no reading in the last 30 min. Consumers fail open: they run, but warn.
- **A window that has reset counts as 0**, whatever the old reading said.

How readings arrive:
- The status line saves a reading on every interactive turn, which costs nothing.
- When the last reading is over 10 minutes old, the poller reads the usage endpoint instead.

Crossing 75% or 90% on either limit sends one Slack message per window, through the same
optional Slack helper as the PR reviewer (`REVIEW_SLACK_SCRIPT` / `REVIEW_SLACK_ENV`).

### Commands
- `usage status`: current limits and gate.
- `usage override 2h | 30m | until-reset | off`: let gated work run past 75%.
- `usage gate --source NAME`: for scripts. Exit 0 means go, exit 3 means paused.
- **Scripts that can't call the CLI** can read `gate.json` directly. Treat `computed_at` older
  than 30 min as `unknown`.

### Who reads the gate
- **Every Claude session** (`claude/hooks/usage-flag.py`):
  - At `flag`, the first prompt of each session in each window gets a visible warning, and
    Claude is told to avoid heavy work.
  - While flagged, subagents, workflows, `claude -p`, `git push` and
    `gh pr create` ask for approval.
- **The PR reviewer:** timer runs skip while flagged and log "governor: pausing reviews" once.
  A manual `--pr N` still runs.
- **Sweep and fix:** `common.claude_json` raises "usage limit: governor paused…", which fix.py
  already treats as a clean stop.
- **An override (`usage override 2h`) lifts all of these.**

### Installing
`install.sh` does three things:
- symlinks `usage` into `~/.local/bin`
- sets the Claude Code `statusLine` to `usage statusline`, only if none is set
- enables `usage-poll.timer`


`bin/usage report [--since 7d] [--top 15] [--out FILE]` scans every Claude Code transcript on
this box into a local SQLite ledger (`~/.local/state/usage/usage.db`), then reports tokens and
API-equivalent cost by source, model, session, 5-hour window and day. `bin/usage scan` only
updates the ledger. The scanner is incremental; a full backfill of thousands of calls takes a
few seconds.

## Where the numbers come from
- **Transcripts:** `~/.claude/projects/**/*.jsonl`, scanned recursively so subagent files are
  included. Each API call appears on several lines while it streams, so calls are keyed by
  `requestId`.
- **Prices:** `prices.toml`, list prices. Subscription usage is reported as what the same
  tokens would cost on the API, so sources compare on one axis.
- **Attribution:** `sources.toml` holds ordered regex rules over the call's working directory
  and entrypoint (`cli` = interactive, `sdk-cli` = `claude -p`). Attribution happens at report
  time, so editing the rules needs no rescan.
- **Tags:** headless wrappers that pass `claude --session-id <uuid>` can write
  `{session_id, source}` to `~/.local/state/usage/tags.jsonl`. A tag beats any rule. This is
  how runs that share one directory, such as several cron jobs, are told apart.
- **Events:** programs that keep no transcript (`--no-session-persistence`) append call records
  to `~/.local/state/usage/events.jsonl`:
  `{request_id, ts, source, model, usage: {input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cache_write_1h_tokens}}`

## Checks
- **Review costs:** the ledger's review totals can be checked against the reviewer's own log
  (`~/.local/state/pr-review/log`).
- **Tests:** `python3 -m unittest discover -s usage/tests -t .`

## Live limits
`GET https://api.anthropic.com/api/oauth/usage` works with the Claude Code login token
(`claudeAiOauth.accessToken` in `~/.claude/.credentials.json`, header
`anthropic-beta: oauth-2025-04-20`). It is undocumented, so treat it as best-effort.

What it returns:

- `five_hour.{utilization, resets_at}` and `seven_day.{utilization, resets_at}`: the same
  numbers as `/usage`.
- `limits[]`: one entry per limit, with `kind` (`session`, `weekly_all`, `weekly_scoped` for a
  model family), `percent`, `severity`, `resets_at` and `is_active`. `is_active` marks the limit
  that is currently binding.
- `seven_day_breakdown.rows[]`: the weekly share by surface (Claude Code, Chats, Cowork, …).

The weekly limit can be the binding one even when the 5-hour window is low, so the governor
watches both.

The Claude Code status-line input also carries `rate_limits.five_hour` and `seven_day`
(`used_percentage`, `resets_at`), for free on every interactive turn. The governor uses it as the
primary reading and this endpoint as the fallback.
