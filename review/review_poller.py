#!/usr/bin/env python3
"""Review open PRs in the watched repos. Run every 2 min by pr-review.timer.

For each open, non-draft PR by REVIEW_OWNER whose head SHA hasn't been reviewed yet:
  1. check the PR out in a cache clone (~/.cache/pr-review/<repo>); the live ~/dev checkouts,
     several of which ARE the running services, are never touched
  2. run `claude -p` with read-only tools and a JSON schema for the findings
  3. post the findings as one PR comment (see common.py for the comment contract)

Claude never gets write access. This script is the only thing that posts anything.

    review_poller.py                         # normal timer run
    review_poller.py --repo my-repo --pr 7 --dry-run   # print, don't post or record
    review_poller.py --repo example-web --pr 4 --force    # review even if already reviewed
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CACHE, HERE, MAX_REVIEWS, OWNER, STATE, encode_data, parse_review,  # noqa: E402
                    pr_comments, request_file, run, watched_repos)

CLAUDE = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))
MODEL = os.environ.get("REVIEW_MODEL")  # unset = the CLI's default model
REVIEW_TIMEOUT = 20 * 60
MAX_FAILURES = 3  # per head SHA; alert on the first, give up after this many
# Optional Slack alerts: a script called as `node <script> <headline> 1` with the detail on stdin,
# and an env file holding SLACK_WEBHOOK_URL. Unset or missing = no alerts.
SLACK = Path(os.environ.get("REVIEW_SLACK_SCRIPT", Path.home() / "dev/notify/notify-slack.js"))
CRON_ENV = Path(os.environ.get("REVIEW_SLACK_ENV", Path.home() / "dev/notify/.env"))

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "verdict": {"type": "string", "enum": ["changes", "clean"]},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "problem": {"type": "string"},
                "recommendation": {"type": "string"},
                "patch": {"type": "string"},
            },
            "required": ["severity", "file", "line", "problem", "recommendation"],
        }},
    },
    "required": ["summary", "verdict", "findings"],
}
READ_ONLY = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
             "Bash(git blame:*)", "Bash(git status:*)", "Bash(ls:*)"]
SEV_ICON = {"blocker": "🛑", "major": "🟠", "minor": "🟡", "nit": "⚪"}


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    with open(STATE / "log", "a") as f:
        f.write(line + "\n")


def alert(headline, detail):
    """Slack alert via an external webhook helper (REVIEW_SLACK_SCRIPT). Best-effort, never raises."""
    if not SLACK.exists():
        return
    try:
        env = dict(os.environ)
        if CRON_ENV.exists():
            for line in CRON_ENV.read_text().splitlines():
                if line.startswith("SLACK_WEBHOOK_URL="):
                    env["SLACK_WEBHOOK_URL"] = line.split("=", 1)[1].strip().strip('"\'')
        subprocess.run(["node", str(SLACK), headline, "1"], input=detail[-2500:], text=True,
                       env=env, timeout=30, capture_output=True)
    except Exception as e:  # noqa: BLE001
        log(f"alert failed: {e}")


def load_state(key):
    f = STATE / f"{key}.json"
    return json.loads(f.read_text()) if f.exists() else {"reviews": [], "failures": {}}


def save_state(key, st):
    (STATE / f"{key}.json").write_text(json.dumps(st, indent=2))


def since_cutoff():
    """PRs opened before the reviewer was first enabled are left alone (use --pr to force one)."""
    f = STATE / "config.json"
    if not f.exists():
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"since": datetime.now(timezone.utc).isoformat()}))
    return json.loads(f.read_text())["since"]


def checkout(slug, number, base, sha):
    d = CACHE / slug.split("/")[1]
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", slug, str(d), "--", "--quiet"])
    run(["git", "fetch", "--quiet", "--force", "origin",
         f"+refs/heads/{base}:refs/remotes/origin/{base}", f"+refs/pull/{number}/head:refs/pr/{number}"], cwd=d)
    run(["git", "checkout", "--quiet", "--force", "--detach", sha], cwd=d)
    run(["git", "clean", "-fdxq"], cwd=d)
    return d


def prior_rounds_text(slug, number):
    """Earlier reviews and the author's replies since, so round 2+ doesn't re-raise rejected items."""
    parts = []
    for c in pr_comments(slug, number):
        rv = parse_review(c["body"])
        if rv and rv[1]:
            parts.append(f"### Your review of {rv[0]['sha'][:8]} (round {rv[0]['round']})\n"
                         + json.dumps(rv[1].get("findings", []), indent=1))
        elif parts:  # an author reply after a review
            parts.append(f"### Author reply ({c['createdAt']})\n{c['body']}")
    if not parts:
        return ""
    return ("## Earlier rounds\nYou've reviewed this PR before. Below are your earlier findings and the "
            "author's replies. Check that applied fixes are correct, and don't repeat rejected "
            "findings unless the author's reason is wrong.\n\n" + "\n\n".join(parts))


def review(slug, pr, rnd, workdir):
    prompt = (HERE / "review-prompt.md").read_text().format(
        repo=slug, number=pr["number"], title=pr["title"], base=pr["baseRefName"], sha=pr["headRefOid"],
        round=rnd, max_rounds=MAX_REVIEWS, body=(pr.get("body") or "(none)")[:8000],
        prior_rounds=prior_rounds_text(slug, pr["number"]) if rnd > 1 else "")
    # Bill the subscription login, like the cron jobs: drop an inherited API key and the
    # parent-session markers that would make this look like a nested session.
    env = {k: v for k, v in os.environ.items()
           if k != "ANTHROPIC_API_KEY" and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    cmd = [CLAUDE, "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--tools", "Read,Grep,Glob,Bash", "--allowedTools", *READ_ONLY, "--permission-mode", "dontAsk"]
    if MODEL:
        cmd += ["--model", MODEL]
    p = run(cmd, cwd=workdir, input=prompt, env=env, check=False, timeout=REVIEW_TIMEOUT)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p exit {p.returncode}, non-JSON output:\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude -p failed ({out.get('subtype')}): {str(out.get('result'))[-1500:]}")
    res = out["structured_output"]
    # Verdict follows from severities, whatever the model said.
    res["verdict"] = "changes" if any(f["severity"] in ("blocker", "major") for f in res["findings"]) else "clean"
    res["cost_usd"] = out.get("total_cost_usd")
    return res


def render(pr, rnd, res):
    sha = pr["headRefOid"]
    head = "changes requested" if res["verdict"] == "changes" else "no blocking issues"
    lines = [f"<!-- claude-review sha={sha} round={rnd} verdict={res['verdict']} -->",
             f"## 🔍 Claude review: round {rnd}/{MAX_REVIEWS}, {head}",
             f"Commit `{sha[:8]}`.\n", res["summary"], ""]
    if res["findings"]:
        lines += ["| # | Sev | Where | Problem | Recommendation |", "|---|---|---|---|---|"]
        for i, f in enumerate(res["findings"], 1):
            cell = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
            where = f"`{f['file']}:{f['line']}`" if f["line"] else f"`{f['file']}`"
            lines.append(f"| {i} | {SEV_ICON[f['severity']]} {f['severity']} | {where} | "
                         f"{cell(f['problem'])} | {cell(f['recommendation'])} |")
        patches = [(i, f["patch"]) for i, f in enumerate(res["findings"], 1) if f.get("patch")]
        if patches:
            lines += ["", "<details><summary>Suggested patches</summary>", ""]
            for i, patch in patches:
                lines += [f"**#{i}**", "```diff", patch.strip(), "```"]
            lines += ["</details>"]
    lines += ["", "_The authoring session applies or rebuts these. Reply here to rebut a finding; "
              "the next round reads replies._",
              f"<!-- claude-review-data {encode_data({'sha': sha, 'round': rnd, **res})} -->"]
    return "\n".join(lines)


def handle(dirname, slug, pr, args, since):
    n, sha = pr["number"], pr["headRefOid"]
    key = f"{slug.split('/')[1]}-{n}"
    st = load_state(key)
    forced = args.pr == n
    if not forced:
        if pr["isDraft"] or any(lb["name"] == "no-review" for lb in pr["labels"]):
            return
        if pr["createdAt"] < since and not request_file(slug, n).exists():
            return
        if any(r["sha"] == sha for r in st["reviews"]) or st.get("capped_sha") == sha:
            return
        if st["failures"].get(sha, 0) >= MAX_FAILURES:
            return
    if forced and not args.force and any(r["sha"] == sha for r in st["reviews"]):
        log(f"{key}: {sha[:8]} already reviewed (use --force)")
        return
    rnd = len(st["reviews"]) + 1
    if rnd > MAX_REVIEWS and not args.force:
        if not args.dry_run:
            run(["gh", "pr", "comment", str(n), "-R", slug, "--body",
                 f"<!-- claude-review sha={sha} round=cap verdict=cap -->\n"
                 f"## 🔍 Claude review: cap reached\nThis PR has had {MAX_REVIEWS} automated reviews. "
                 f"Commit `{sha[:8]}` needs a human look."])
            st["capped_sha"] = sha
            save_state(key, st)
        log(f"{key}: review cap reached at {sha[:8]}")
        return

    log(f"{key}: reviewing {sha[:8]} round {rnd}")
    t0 = time.time()
    try:
        res = review(slug, pr, rnd, checkout(slug, n, pr["baseRefName"], sha))
    except Exception as e:  # noqa: BLE001
        st["failures"][sha] = st["failures"].get(sha, 0) + 1
        if not args.dry_run:
            save_state(key, st)
        log(f"{key}: FAILED ({st['failures'][sha]}/{MAX_FAILURES}): {e}")
        if st["failures"][sha] == 1 and not args.dry_run:
            alert(f"PR reviewer failed on {slug}#{n}", str(e))
        return
    body = render(pr, rnd, res)
    log(f"{key}: {res['verdict']}, {len(res['findings'])} findings, "
        f"{time.time() - t0:.0f}s, ${res.get('cost_usd') or 0:.2f}")
    if args.dry_run:
        print(body)
        return
    run(["gh", "pr", "comment", str(n), "-R", slug, "--body-file", "-"], input=body)
    st["reviews"].append({"sha": sha, "round": rnd, "verdict": res["verdict"],
                          "at": datetime.now(timezone.utc).isoformat()})
    save_state(key, st)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="~/dev dir name; limits the run to this repo")
    ap.add_argument("--pr", type=int, help="review this PR even if it predates the cutoff")
    ap.add_argument("--dry-run", action="store_true", help="print the comment; don't post or record")
    ap.add_argument("--force", action="store_true", help="re-review an already reviewed head / ignore cap")
    args = ap.parse_args()
    if args.pr and not args.repo:
        ap.error("--pr needs --repo")

    STATE.mkdir(parents=True, exist_ok=True)
    lock = open(STATE / "poller.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another poller run is active", file=sys.stderr)
        return
    since = since_cutoff()
    repos = watched_repos()
    if args.repo:
        if args.repo not in repos:
            sys.exit(f"{args.repo} isn't watched; add it to review/repos.txt")
        repos = {args.repo: repos[args.repo]}
    for dirname, slug in repos.items():
        try:
            prs = json.loads(run(["gh", "pr", "list", "-R", slug, "--state", "open", "--author", OWNER,
                                  "--json", "number,title,body,headRefOid,baseRefName,isDraft,labels,createdAt"]).stdout)
        except Exception as e:  # noqa: BLE001
            log(f"{slug}: pr list failed: {e}")
            continue
        for pr in prs:
            if args.pr and pr["number"] != args.pr:
                continue
            handle(dirname, slug, pr, args, since)


if __name__ == "__main__":
    main()
