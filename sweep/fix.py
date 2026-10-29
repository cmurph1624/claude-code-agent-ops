#!/usr/bin/env python3
"""Headless fixer for code-sweep issues. For each open blocker/major `claude-sweep` issue, in order:
  1. a fresh worktree on origin/<base> under ~/.cache/sweep-fix/ (live checkouts are never touched)
  2. `claude -p` fixes it (edits plus tests/lint only; no git, no network, no deploys)
  3. the script gates on tests and lint, commits, pushes, and opens a PR that says "Fixes #N"
  4. the PR reviewer (pr-review.timer) reviews it, and the fixer addresses findings for up to 2 rounds
  5. the PR is left open for the user to merge. The fixer never merges (in some repos a merge deploys).

    fix.py example-web --max 5
    fix.py example-web --issue 12        # just this issue

Run it detached so it outlives the terminal:
    systemd-run --user --unit=sweep-fix-example-web --property=TimeoutStartSec=infinity \\
        python3 ~/dev/agent-ops/sweep/fix.py example-web --max 5
Log: ~/.local/state/sweep/<Repo>/fix.log
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
from common import CACHE, STATE, claude_json, run  # noqa: E402

HERE = Path(__file__).resolve().parent
REVIEW_DATA = HERE.parent / "review" / "review_data.py"
MAX_FIX_ROUNDS = 2
TRAILER = "\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
NODE_ENV = {**os.environ, "NODE_ENV": "development"}  # this box sets production; dev deps are needed
EDIT_TOOLS = "Read,Edit,Write,Glob,Grep,Bash"
EDIT_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(npm test:*)", "Bash(npm run lint:*)",
                "Bash(npx vitest:*)", "Bash(npx tsc:*)", "Bash(npx eslint:*)", "Bash(git diff:*)",
                "Bash(git status:*)", "Bash(ls:*)"]

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["fixed", "not_fixed"]},
        "summary": {"type": "string", "description": "what you changed and why, or why you didn't"},
        "pr_body": {"type": "string", "description": "PR description: the problem, the fix, how it was tested"},
        "needs_migration": {"type": "boolean"},
    },
    "required": ["status", "summary", "pr_body", "needs_migration"],
}
ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {
        "replies": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer"}, "action": {"type": "string", "enum": ["applied", "rebutted"]},
            "note": {"type": "string"}}, "required": ["n", "action", "note"]}},
    },
    "required": ["replies"],
}

FIX_PROMPT = """You are fixing one issue in `{repo}`, found by an automated code review. Your working
directory is a fresh worktree on `{base}` with dependencies installed. Read the repo's CLAUDE.md,
AGENTS.md, README and DEPLOY.md first, and follow them.

## Issue #{n}: {title}
{body}

## Rules
- First verify the issue against the code. The reviewer couldn't run anything and can be wrong.
  If it's wrong, or already fixed, change nothing: return status `not_fixed` and explain why.
- Make the smallest change that fixes it properly, and add or adjust tests that would have caught it.
- Run `npm test` and `npm run lint`, and make them pass. Don't weaken or skip tests to get there.
- Don't run git commands that change state, don't touch the network, and don't deploy. The caller
  commits, pushes and opens the PR.
- If the fix needs a database change, add a new migration file under supabase/migrations following the
  existing naming. Never apply it. Set needs_migration=true and explain it in pr_body.
- If the issue needs a product decision rather than a code fix, return `not_fixed` and state the decision needed.
"""
ADDRESS_PROMPT = """You opened PR #{pr} in `{repo}` to fix issue #{n}. The automated reviewer posted these
findings on your latest commit (round {rnd}). The worktree has your PR branch checked out.

{findings}

For each finding: verify it, then apply it if it's right, or rebut it with a concrete reason if it's
wrong. Run `npm test` and `npm run lint` and make them pass. Don't run git commands that change state.
Return one reply per finding, numbered as in the list (1-based).
"""


def log(repo_dir, msg):
    d = STATE.parent / "sweep" / repo_dir
    d.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(d / "fix.log", "a") as f:
        f.write(line + "\n")


def sh_ok(cmd, cwd):
    p = subprocess.run(cmd, shell=True, cwd=cwd, env=NODE_ENV, capture_output=True, text=True, timeout=1800)
    return p.returncode == 0, (p.stdout + p.stderr)[-3000:]


def gates(wt):
    """(tests ok, lint ok, output tail)"""
    t_ok, t_out = sh_ok("npm test --silent", wt)
    l_ok, l_out = sh_ok("npm run lint --silent", wt)
    return t_ok, l_ok, (t_out if not t_ok else l_out)


def pick_issues(repo, only, limit):
    out = json.loads(run(["gh", "issue", "list", "-R", repo, "--label", "claude-sweep", "--state", "open",
                          "--limit", "500", "--json", "number,title,body,labels"]).stdout)
    rank = {"sev:blocker": 0, "sev:major": 1}
    picked = []
    for i in out:
        labels = {lb["name"] for lb in i["labels"]}
        sev = min((rank[lb] for lb in labels if lb in rank), default=None)
        if only and i["number"] not in only:
            continue
        if sev is None or "fix-in-progress" in labels:
            continue
        picked.append((sev, i["number"], i))
    return [i for _, _, i in sorted(picked, key=lambda t: (t[0], t[1]))][:limit]


def fix_one(cfg, repo_dir, issue):
    repo, base, n = cfg["repo"], cfg["base"], issue["number"]
    L = lambda m: log(repo_dir, f"#{n}: {m}")  # noqa: E731
    run(["gh", "issue", "edit", str(n), "-R", repo, "--add-label", "fix-in-progress"])
    clone = CACHE.parent / "sweep" / repo.split("/")[1]
    wt = CACHE.parent / "sweep-fix" / f"{repo.split('/')[1]}-{n}"
    branch = f"claude/sweep-fix-{n}"
    run(["git", "fetch", "--quiet", "origin", base], cwd=clone)
    if wt.exists():
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=clone, check=False)
    # A PR left by an earlier run (interrupted by a usage limit, say) is resumed, not re-opened.
    existing = json.loads(run(["gh", "pr", "list", "-R", repo, "--head", branch, "--state", "open",
                               "--json", "number,url"]).stdout)
    if existing:
        run(["git", "fetch", "--quiet", "origin", branch], cwd=clone)
    run(["git", "worktree", "add", "--quiet", "-B", branch, str(wt),
         f"origin/{branch}" if existing else f"origin/{base}"], cwd=clone)
    try:
        L("installing deps")
        ok, out = sh_ok("npm ci --include=dev --no-audit --no-fund --silent", wt)
        if not ok:
            raise RuntimeError(f"npm ci failed:\n{out}")
        base_t, base_l, _ = gates(wt)
        L(f"baseline tests={'pass' if base_t else 'FAIL'} lint={'pass' if base_l else 'FAIL'}")
        if existing:
            pr = existing[0]["number"]
            done = run(["git", "log", "--format=%s", f"origin/{base}..HEAD"], cwd=wt).stdout
            first_round = sum(s.startswith("Address review round") for s in done.splitlines())
            L(f"resuming {existing[0]['url']} after {first_round} fix round(s)")
            review_loop(repo, n, pr, wt, base_t, base_l, first_round, L)
            return

        L("fixing")
        res, raw = claude_json(FIX_PROMPT.format(repo=repo, base=base, n=n, title=issue["title"], body=issue["body"]),
                               wt, FIX_SCHEMA, EDIT_TOOLS, EDIT_ALLOWED, mode="acceptEdits", timeout=45 * 60)
        L(f"claude: {res['status']} (${raw.get('total_cost_usd') or 0:.2f})")
        changed = run(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
        if res["status"] != "fixed" or not changed:
            run(["gh", "issue", "comment", str(n), "-R", repo, "--body-file", "-"],
                input=f"🤖 Auto-fixer didn't change anything for this issue.\n\n{res['summary']}")
            run(["gh", "issue", "edit", str(n), "-R", repo, "--remove-label", "fix-in-progress"])
            return
        t_ok, l_ok, tail = gates(wt)
        if (base_t and not t_ok) or (base_l and not l_ok):
            run(["gh", "issue", "comment", str(n), "-R", repo, "--body-file", "-"],
                input=f"🤖 Auto-fixer's change broke {'tests' if base_t and not t_ok else 'lint'}, so no PR was opened. "
                      f"It needs a human or a session.\n\n```\n{tail[-1500:]}\n```")
            run(["gh", "issue", "edit", str(n), "-R", repo, "--remove-label", "fix-in-progress"])
            L("gates failed; no PR")
            return

        run(["git", "add", "-A"], cwd=wt)
        run(["git", "commit", "-q", "-m", f"Fix #{n}: {issue['title']}{TRAILER}"], cwd=wt)
        run(["git", "push", "-q", "--force-with-lease", "-u", "origin", branch], cwd=wt)
        body = res["pr_body"] + f"\n\nFixes #{n}"
        if res["needs_migration"]:
            body += "\n\n⚠️ **Includes a Supabase migration. It is not applied; apply it by hand before or with the merge.**"
        if not base_t or not base_l:
            body += "\n\nℹ️ Tests or lint were already failing on the base branch before this change."
        body += "\n\n🤖 Opened by the code-sweep auto-fixer (agent-ops/sweep/fix.py). Merging is up to you."
        pr_url = run(["gh", "pr", "create", "-R", repo, "--base", base, "--head", branch,
                      "--title", f"Fix #{n}: {issue['title']}"[:120], "--body-file", "-"], input=body).stdout.strip()
        pr = int(pr_url.rstrip("/").split("/")[-1])
        L(f"opened {pr_url}")
        review_loop(repo, n, pr, wt, base_t, base_l, 0, L)
    finally:
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=clone, check=False)
        shutil.rmtree(wt, ignore_errors=True)


def review_loop(repo, n, pr, wt, base_t, base_l, first_round, L):
    """Wait for each review and address it, for fix rounds first_round..MAX_FIX_ROUNDS-1."""
    verdict = None
    for rnd in range(first_round, MAX_FIX_ROUNDS + 1):
        p = subprocess.run([sys.executable, str(REVIEW_DATA), str(pr), "--repo", repo, "--wait", "1800"],
                           capture_output=True, text=True)
        if p.returncode != 0:
            L(f"no review (exit {p.returncode}): {p.stderr.strip()[:200]}")
            break
        rv = json.loads(p.stdout)
        verdict = rv["verdict"]
        L(f"review round {rv.get('round')}: {verdict}, {len(rv.get('findings', []))} findings")
        if verdict == "clean" or rnd == MAX_FIX_ROUNDS:
            break
        findings = "\n".join(f"{i}. [{f['severity']}] {f['file']}:{f['line']}: {f['problem']}\n   Recommendation: {f['recommendation']}"
                             for i, f in enumerate(rv["findings"], 1))
        ans, raw = claude_json(ADDRESS_PROMPT.format(pr=pr, repo=repo, n=n, rnd=rnd + 1, findings=findings),
                               wt, ADDRESS_SCHEMA, EDIT_TOOLS, EDIT_ALLOWED, mode="acceptEdits", timeout=45 * 60)
        replies = "\n".join(f"#{r['n']} {r['action']}: {r['note']}" for r in ans["replies"])
        if run(["git", "status", "--porcelain"], cwd=wt).stdout.strip():
            t_ok, l_ok, tail = gates(wt)
            if (base_t and not t_ok) or (base_l and not l_ok):
                run(["git", "checkout", "--", "."], cwd=wt)
                replies += f"\n\n⚠️ The round-{rnd + 1} changes broke tests or lint, so they were discarded."
                run(["gh", "pr", "comment", str(pr), "-R", repo, "--body-file", "-"], input=replies)
                break
            run(["git", "commit", "-qam", f"Address review round {rnd + 1}{TRAILER}"], cwd=wt)
            run(["git", "push", "-q"], cwd=wt)
            run(["gh", "pr", "comment", str(pr), "-R", repo, "--body-file", "-"], input=replies)
        else:  # everything rebutted: nothing new to review
            run(["gh", "pr", "comment", str(pr), "-R", repo, "--body-file", "-"], input=replies)
            break
    status = ("✅ Review is clean. Ready for you to merge (merging deploys)." if verdict == "clean"
              else "⚠️ Still has open review findings. Needs a look before merging.")
    run(["gh", "pr", "comment", str(pr), "-R", repo, "--body", f"🤖 Auto-fixer done: {status}"])
    L(f"done: {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--max", type=int, default=5)
    ap.add_argument("--issue", type=int, action="append")
    a = ap.parse_args()
    cfg = yaml.safe_load((HERE / "areas" / f"{a.repo}.yaml").read_text())
    issues = pick_issues(cfg["repo"], a.issue, a.max)
    log(a.repo, f"fixer start: {[i['number'] for i in issues]}")
    for issue in issues:
        try:
            fix_one(cfg, a.repo, issue)
        except Exception as e:  # noqa: BLE001
            log(a.repo, f"#{issue['number']}: FAILED: {e}")
            run(["gh", "issue", "edit", str(issue["number"]), "-R", cfg["repo"], "--remove-label", "fix-in-progress"], check=False)
            if re.search(r"session limit|usage limit|rate limit|credit balance", str(e), re.I):
                log(a.repo, "usage limit hit: stopping. Re-run after the reset; open PRs are resumed.")
                break
    log(a.repo, "fixer finished")


if __name__ == "__main__":
    main()
