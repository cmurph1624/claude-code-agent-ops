#!/usr/bin/env python3
"""Fetch (or wait for) the reviewer's findings on a PR. Used by the pr-review-loop skill.

    review_data.py <pr> [--repo owner/name] [--sha <head>] [--wait <seconds>]

Run it from inside the repo, or pass --repo. --sha defaults to the PR's current head commit.
It prints the review for that commit as JSON:
    {"sha", "round", "verdict", "summary", "findings": [...], "comment_url"}
Exit status: 0 = found, 2 = the reviewer hit its cap, 3 = no review yet (or timed out),
4 = the repo isn't watched.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import parse_review, pr_comments, request_file, run, slug_for_dir  # noqa: E402


def find(slug, number, sha):
    for c in reversed(pr_comments(slug, number)):
        rv = parse_review(c["body"])
        if rv and rv[0]["sha"] == sha:
            meta, data = rv
            if meta["verdict"] == "cap":
                return 2, {"sha": sha, "verdict": "cap", "comment_url": c.get("url")}
            return 0, {**(data or {}), "verdict": meta["verdict"], "comment_url": c.get("url")}
    return 3, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pr", type=int)
    ap.add_argument("--repo")
    ap.add_argument("--sha")
    ap.add_argument("--wait", type=int, default=0, help="poll up to this many seconds")
    a = ap.parse_args()
    slug = a.repo or slug_for_dir(Path.cwd())
    if not slug:
        print("This repo isn't watched by the reviewer (agent-ops/review/repos.txt).", file=sys.stderr)
        sys.exit(4)
    sha = a.sha or json.loads(run(["gh", "pr", "view", str(a.pr), "-R", slug,
                                   "--json", "headRefOid"]).stdout)["headRefOid"]
    deadline = time.time() + a.wait
    if a.wait:  # someone is waiting: make sure the timer reviews it even if it predates the cutoff
        request_file(slug, a.pr).parent.mkdir(parents=True, exist_ok=True)
        request_file(slug, a.pr).touch()
    while True:
        code, out = find(slug, a.pr, sha)
        if code != 3 or time.time() >= deadline:
            break
        time.sleep(30)
    if out:
        print(json.dumps(out, indent=1))
    else:
        print(f"No review yet for {slug}#{a.pr} at {sha[:8]}.", file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
