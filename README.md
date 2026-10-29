# agent-ops

Guardrails and automation for running Claude Code sessions against real projects on a home server.

When several Claude Code sessions write code across a dozen repos, two things go wrong first:
nobody reviews the PRs, and "deploy" turns into whatever shell command a session thinks of. This
repo is the layer that handles both. Every PR gets an automated review that the authoring session
has to answer. Deploys go through one registry, one agent, and a hook that makes every
deploy-shaped command ask a human first.

It's built for one person's setup (one server, repos under `~/dev`, one GitHub owner), but each
piece is small and the seams are explicit, so it adapts to other setups.

## How the pieces fit

```
 author session                    reviewer (systemd timer, every 2 min)
 ──────────────                    ──────────────────────────────────────
 gh pr create / git push ──hook──▶ on-pr-push.py: "run pr-review-loop"
        │                                         │
 pr-review-loop skill                review_poller.py finds the new head commit
   waits: review_data.py --wait ◀──── claude -p, read-only tools, clean cache clone
   applies / rebuts each finding      posts ONE PR comment: findings + hidden JSON
   pushes, replies "#1 applied…"      (round n of 3; then a "cap reached" comment)
   merges on a clean verdict
        │
 deploy skill / ops agent ──────▶ ops/deployments.yaml: how each project deploys,
   (only on an explicit ask)        health checks, rollback, cautions
        │
 every Bash / Supabase / Artifact call ──▶ deploy-gate.py: deploy-shaped? → permission prompt
```

### 1. Automated PR review (`review/`)
`pr-review.timer` runs `review_poller.py` every 2 minutes. For each open PR in a watched repo
(`review/repos.txt`, `*` for all), it reviews every new head commit. Each review runs `claude -p`
in a clean cache clone, limited to read-only tools (`Read`, `Grep`, `Glob`, and an allowlist of
read-only Bash such as `git diff` and `git log`). Output is forced through a JSON schema: a
summary, a verdict, and findings with severity, file:line, problem, recommendation and an
optional patch. The verdict is computed from the severities, not taken from the model. Results
are posted as one PR comment with the structured data embedded, so the author session reads JSON
instead of scraping markdown.

Each PR gets at most **3 reviews**: the first, plus 2 after fix rounds. After that the poller
posts a "cap reached" comment, and the PR needs a human. Drafts and PRs labeled `no-review` are
skipped. A failing review alerts Slack (through an external notify script) on the first failure, and
the poller gives up on that commit after 3.

### 2. Hand-off to the author (`claude/skills/pr-review-loop`, `claude/hooks/on-pr-push.py`)
After `gh pr create` or `git push`, a PostToolUse hook tells the session to run the
**pr-review-loop** skill. The skill waits for the review (`review_data.py --wait`, exit codes
0 found / 2 cap / 3 timeout / 4 unwatched). It verifies each finding against the code, applies or
rebuts it, runs tests, pushes, and replies on the PR with numbered `applied` / `rebutted` lines,
which the next round reads. It stops after 2 fix rounds. It merges only on a clean verdict, and
never if the PR is labeled `hold`.

### 3. Deploys (`ops/`, `claude/agents/ops.md`, `claude/skills/deploy`)
`ops/deployments.yaml` is the one registry for every project: deploy mechanism (GitHub Actions
dispatch, script, compose, Vercel, claude.ai artifact), the deploy steps, health check, rollback,
`confirm` items that need explicit user approval, and `cautions`. The **ops** agent deploys only
what was explicitly asked. It runs preflight, records the previous ref, deploys, health-checks,
and rolls back automatically on failure. `ops/status.py` prints deployed ref vs origin, drift and
health for everything.

### 4. Deploy gate (`claude/hooks/deploy-gate.py`)
A PreToolUse hook that turns deploy-shaped actions into a permission prompt in **every** session
and permission mode, including auto mode and the ops agent. A "yes, deploy it" in chat isn't
enough; the user approves the actual command. It gates:

- deploy and install scripts, and `gh workflow run`
- start/stop/restart of deployed units
- `docker compose up/down`
- `gh pr merge` where merging deploys (`merge_deploys: true`)
- git checkout/pull/reset in a **live checkout**, where the service runs from the working tree,
  so changing files *is* the deploy (`live_checkout: true`)
- Supabase writes, both CLI and MCP (reads pass)
- Artifact publishes for artifact-hosted projects

It fails open: an error in the hook never blocks normal work.

### 5. One-time code sweep (`sweep/`)
`sweep.py <Repo>` reviews a whole repo area by area (`sweep/areas/<Repo>.yaml`) and files GitHub
issues: one per blocker or major finding, and a roll-up per area for minor ones. `fix.py <Repo>
--max N` then fixes blocker and major issues one PR at a time: worktree, headless fix, test and
lint gates, PR, and up to 2 review rounds. It never merges.

## Install

Requirements: Linux with systemd user units, Python 3 with PyYAML, the `gh` CLI (authenticated),
and the `claude` CLI.

```bash
git clone https://github.com/cmurph1624/claude-code-agent-ops ~/dev/agent-ops
cd ~/dev/agent-ops && ./install.sh
```

`install.sh` is idempotent. It:

- symlinks the skills and the ops agent into `~/.claude`
- adds an `@import` of `claude/CLAUDE.global.md` to `~/.claude/CLAUDE.md`
- merges the hooks into `~/.claude/settings.json` (backing it up first, keeping existing hooks)
- enables the reviewer timer

To adapt it to your setup:

- `REVIEW_OWNER` env var (your GitHub user; whose PRs get reviewed), and `DEV` in `review/common.py` (where your checkouts live)
- `review/repos.txt`: which repos to review (ships with examples)
- `ops/deployments.yaml`: your projects. It ships with three example projects; the field reference is at the top of the file
- `claude/CLAUDE.global.md`: the working rules every session loads
- `claude/hooks/deploy-gate.py`: `UNITS`, the systemd units that count as deployed services
- `sweep/areas/<repo>.yaml`: areas for a one-time codebase sweep (see `example-web.yaml`)
- `REVIEW_SLACK_SCRIPT` / `REVIEW_SLACK_ENV` env vars: an optional failure-alert script and the env file holding `SLACK_WEBHOOK_URL`

## Day to day

```bash
python3 review/review_poller.py --repo <dir> --pr <n> --dry-run   # preview a review
python3 review/review_poller.py --repo <dir> --pr <n> --force     # re-review now, ignoring the cap
python3 review/review_data.py <n>                                 # latest findings for a PR (run in the repo)
tail -f ~/.local/state/pr-review/log
python3 ops/status.py                                             # drift + health for every project
systemctl --user stop pr-review.timer                             # pause reviews
```

`REVIEW_MODEL` overrides the reviewer's model (unset means the CLI default).
