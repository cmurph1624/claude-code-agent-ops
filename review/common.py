"""Shared bits for the PR reviewer, its hand-off tooling and the hook.

The review comment is the hand-off contract between the reviewer and the authoring session:
a visible markdown table for humans plus two HTML comments for machines:

    <!-- claude-review sha=<head sha> round=<n> verdict=<changes|clean|cap> -->
    <!-- claude-review-data <base64 JSON> -->

The JSON is base64'd so nothing in a finding (a stray `-->`) can break out of the comment.
"""
import base64
import json
import os
import re
import subprocess
from pathlib import Path

DEV = Path.home() / "dev"
HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pr-review"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pr-review"
OWNER = os.environ.get("REVIEW_OWNER", "your-github-user")  # whose PRs get reviewed
MAX_REVIEWS = 3  # initial review + 2 re-reviews after fix rounds

MARKER_RE = re.compile(r"<!-- claude-review sha=(\w+) round=(\w+) verdict=(\w+) -->")
DATA_RE = re.compile(r"<!-- claude-review-data ([A-Za-z0-9+/=]+) -->")


def run(cmd, cwd=None, check=True, input=None, env=None, timeout=None):
    p = subprocess.run(cmd, cwd=cwd, input=input, env=env, timeout=timeout,
                       capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed ({p.returncode}): {p.stderr.strip()[-2000:]}")
    return p


def origin_slug(path):
    p = run(["git", "-C", str(path), "remote", "get-url", "origin"], check=False)
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", p.stdout.strip())
    return m.group(1) if m else None


def watched_repos():
    """{dir name: owner/repo} for the repos named in repos.txt (see that file for syntax)."""
    include, exclude, star = set(), set(), False
    for line in (HERE / "repos.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line == "*":
            star = True
        elif line.startswith("!"):
            exclude.add(line[1:])
        else:
            include.add(line)
    repos, seen = {}, set()
    # Sort so a dir named after its repo wins over a worktree of it (my-repo vs my-repo-feature).
    dirs = sorted((d for d in DEV.iterdir() if (d / ".git").exists()),
                  key=lambda d: (origin_slug(d) or "", d.name != (origin_slug(d) or "/").split("/")[1]))
    for d in dirs:
        if d.name in exclude or not (star or d.name in include):
            continue
        slug = origin_slug(d)
        if not slug or not slug.startswith(OWNER + "/") or slug in seen:
            continue
        seen.add(slug)
        repos[d.name] = slug
    return repos


def slug_for_dir(path):
    """owner/repo if `path` is inside a watched repo, else None."""
    top = run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], check=False).stdout.strip()
    if not top:
        return None
    slug = origin_slug(top)
    return slug if slug in watched_repos().values() else None


def request_file(slug, number):
    """A waiting author drops this to ask for a review the timer would otherwise skip (pre-cutoff PRs)."""
    return STATE / "requests" / f"{slug.split('/')[1]}-{number}"


def encode_data(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def parse_review(body):
    """(meta, data) for a review comment, or None if `body` isn't one."""
    m = MARKER_RE.search(body or "")
    if not m:
        return None
    meta = {"sha": m.group(1), "round": m.group(2), "verdict": m.group(3)}
    d = DATA_RE.search(body)
    data = json.loads(base64.b64decode(d.group(1))) if d else None
    return meta, data


def pr_comments(slug, number):
    out = run(["gh", "pr", "view", str(number), "-R", slug, "--json", "comments"]).stdout
    return json.loads(out)["comments"]


CLAUDE = os.environ.get("CLAUDE_BIN", str(Path.home() / ".local/bin/claude"))


def claude_json(prompt, cwd, schema, tools, allowed, mode="dontAsk", timeout=30 * 60, model=None):
    """Run `claude -p` headless and return (structured_output, raw result). Raises on failure.
    Bills the subscription login like the cron jobs: an inherited API key and parent-session
    markers are stripped from the environment."""
    env = {k: v for k, v in os.environ.items()
           if k != "ANTHROPIC_API_KEY" and not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    cmd = [CLAUDE, "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
           "--tools", tools, "--allowedTools", *allowed, "--permission-mode", mode]
    if model:
        cmd += ["--model", model]
    p = run(cmd, cwd=cwd, input=prompt, env=env, check=False, timeout=timeout)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p exit {p.returncode}, non-JSON output:\n{p.stdout[-1500:]}\n{p.stderr[-1500:]}")
    if out.get("is_error") or not out.get("structured_output"):
        raise RuntimeError(f"claude -p failed ({out.get('subtype')}): {str(out.get('result'))[-1500:]}")
    return out["structured_output"], out
