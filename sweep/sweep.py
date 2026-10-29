#!/usr/bin/env python3
"""One-time full-codebase review: split a repo into areas (sweep/areas/<Repo>.yaml), review each
area read-only with `claude -p`, and file the findings as GitHub issues:
  - each blocker/major finding gets its own issue (labels: claude-sweep, sev:<severity>)
  - minor findings and nits go into one roll-up issue per area

    sweep.py example-web --dry-run            # review, print, file nothing
    sweep.py example-web                      # review and file issues
    sweep.py example-web --area money-logic   # just one area

Results are cached per area in ~/.local/state/sweep/<Repo>/<area>.json, so a crash or re-run
doesn't pay for an area twice. --fresh ignores the cache. Issues carry a fingerprint, so
re-filing skips any finding that already has an issue, open or closed.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review"))
from common import CACHE, STATE, claude_json, run  # noqa: E402

HERE = Path(__file__).resolve().parent
SWEEP_STATE = STATE.parent / "sweep"
SEVS = ["blocker", "major", "minor", "nit"]
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "issue title, under 80 chars"},
                "severity": {"type": "string", "enum": SEVS},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "problem": {"type": "string"},
                "recommendation": {"type": "string"},
                "patch": {"type": "string"},
            },
            "required": ["title", "severity", "file", "line", "problem", "recommendation"],
        }},
    },
    "required": ["summary", "findings"],
}
READ_ONLY = ["Read", "Grep", "Glob", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
             "Bash(ls:*)", "Bash(wc:*)"]

PROMPT = """You are doing a full code review of one area of `{repo}`. This is not a PR review: you are
reviewing the code as it stands on `{base}` (commit {sha}). Your working directory is a clean checkout.

## Your area: {area}
Paths: {paths}

Focus: {focus}

Other areas of the codebase are reviewed separately. Read code outside your paths when you need it
to judge your area, but only report findings in your own paths.

## How to review
1. Read the repo's CLAUDE.md, AGENTS.md, README and any DEPLOY.md first. They hold project rules.
2. Read every file in your paths. Skim generated or vendored files and lockfiles.
3. You can't run code. Reason about it carefully, and say so when a finding depends on runtime
   behaviour you couldn't confirm.

## What to report
Report real defects and risks, most severe first:
- **blocker**: a security hole (authz bypass, leaked secret, missing RLS), data loss or corruption,
  or wrong money math that users would see.
- **major**: a likely bug in an edge case, an error path that fails silently, missing tests for
  risky logic, or a race.
- **minor**: clarity, duplication, or a clearly simpler approach.
- **nit**: cheap polish. Use these sparingly.

Each finding becomes a GitHub issue that an automated fixer works from. Make each one self-contained:
the exact file and line, the concrete input or state that triggers the problem, and the exact change
to make. Give each a short issue-style title. Leave out style the linter already enforces, and don't
pad the list. If the area is sound, say so in the summary and return few or no findings.
"""


def fingerprint(f):
    return hashlib.sha1(f"{f['file']}|{f['title'].lower().strip()}".encode()).hexdigest()[:12]


def ensure_labels(repo):
    colors = {"claude-sweep": "5319e7", "sev:blocker": "b60205", "sev:major": "d93f0b",
              "sev:minor": "fbca04", "sev:nit": "c5def5", "fix-in-progress": "0e8a16"}
    for name, color in colors.items():
        run(["gh", "label", "create", name, "-R", repo, "--color", color, "--force"], check=False)


def checkout(repo, base):
    d = CACHE.parent / "sweep" / repo.split("/")[1]
    if not (d / ".git").exists():
        d.parent.mkdir(parents=True, exist_ok=True)
        run(["gh", "repo", "clone", repo, str(d), "--", "--quiet"])
    run(["git", "fetch", "--quiet", "origin", base], cwd=d)
    run(["git", "checkout", "--quiet", "--force", "--detach", f"origin/{base}"], cwd=d)
    run(["git", "clean", "-fdxq"], cwd=d)
    return d, run(["git", "rev-parse", "HEAD"], cwd=d).stdout.strip()


def review_area(cfg, name, area, workdir, sha, fresh):
    cache = SWEEP_STATE / cfg["repo"].split("/")[1] / f"{name}.json"
    if cache.exists() and not fresh:
        res = json.loads(cache.read_text())
        if res.get("sha") == sha:
            print(f"[{name}] cached ({len(res['findings'])} findings)")
            return res
    print(f"[{name}] reviewing {', '.join(area['paths'])}", flush=True)
    t0 = datetime.now()
    prompt = PROMPT.format(repo=cfg["repo"], base=cfg["base"], sha=sha[:8], area=name,
                           paths=", ".join(area["paths"]), focus=area["focus"])
    res, raw = claude_json(prompt, workdir, SCHEMA, "Read,Grep,Glob,Bash", READ_ONLY, timeout=45 * 60)
    res.update(sha=sha, area=name, cost_usd=raw.get("total_cost_usd"))
    res["findings"].sort(key=lambda f: SEVS.index(f["severity"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(res, indent=1))
    counts = {s: sum(f["severity"] == s for f in res["findings"]) for s in SEVS}
    print(f"[{name}] done in {(datetime.now() - t0).seconds}s, ${res['cost_usd'] or 0:.2f}: {counts}", flush=True)
    return res


def finding_md(f, area, sha):
    loc = f"`{f['file']}:{f['line']}`" if f["line"] else f"`{f['file']}`"
    md = [f"**Severity:** {f['severity']} · **Area:** {area} · **Where:** {loc} (at `{sha[:8]}`)", "",
          "### Problem", f["problem"], "", "### Recommendation", f["recommendation"]]
    if f.get("patch"):
        md += ["", "### Suggested patch", "```diff", f["patch"].strip(), "```"]
    return "\n".join(md)


def existing_fingerprints(repo):
    out = run(["gh", "issue", "list", "-R", repo, "--label", "claude-sweep", "--state", "all",
               "--limit", "500", "--json", "body"]).stdout
    fps = set()
    for i in json.loads(out):
        fps.update(l.split("fp=")[1].split()[0] for l in i["body"].splitlines() if "claude-sweep fp=" in l)
    return fps


def create_issue(repo, title, body, labels):
    p = run(["gh", "issue", "create", "-R", repo, "--title", title, "--body-file", "-",
             *sum((["--label", lb] for lb in labels), [])], input=body)
    return p.stdout.strip()


def file_issues(cfg, results):
    repo = cfg["repo"]
    ensure_labels(repo)
    seen = existing_fingerprints(repo)
    for res in results:
        area, sha = res["area"], res["sha"]
        rollup = []
        for f in res["findings"]:
            fp = fingerprint(f)
            if f["severity"] in ("minor", "nit"):
                rollup.append((fp, f))
                continue
            if fp in seen:
                continue
            body = f"<!-- claude-sweep fp={fp} -->\n{finding_md(f, area, sha)}\n\n_Filed by the one-time code sweep (agent-ops/sweep)._"
            url = create_issue(repo, f["title"][:100], body, ["claude-sweep", f"sev:{f['severity']}"])
            print(f"  {f['severity']:7} {url}  {f['title']}")
        fresh = [(fp, f) for fp, f in rollup if fp not in seen]
        if fresh:
            parts = [f"Minor findings and nits from the code sweep of **{area}** (at `{sha[:8]}`). "
                     "Tick them off or close the issue as you see fit.\n"]
            for fp, f in fresh:
                parts += [f"<!-- claude-sweep fp={fp} -->", f"- [ ] **{f['title']}** ({f['severity']}) `{f['file']}:{f['line']}`: "
                          f"{f['problem']}\n  _Fix:_ {f['recommendation']}"]
            url = create_issue(repo, f"Code sweep: minor findings in {area} ({len(fresh)})", "\n".join(parts),
                               ["claude-sweep", "sev:minor"])
            print(f"  roll-up {url}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", help="~/dev dir name, with a matching sweep/areas/<name>.yaml")
    ap.add_argument("--area", action="append", help="only these areas")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="ignore cached area results")
    a = ap.parse_args()
    cfg = yaml.safe_load((HERE / "areas" / f"{a.repo}.yaml").read_text())
    workdir, sha = checkout(cfg["repo"], cfg["base"])
    results = []
    for name, area in cfg["areas"].items():
        if a.area and name not in a.area:
            continue
        try:
            results.append(review_area(cfg, name, area, workdir, sha, a.fresh))
        except Exception as e:  # noqa: BLE001  one bad area shouldn't sink the rest
            print(f"[{name}] FAILED: {e}", flush=True)
    total = sum(r.get("cost_usd") or 0 for r in results)
    print(f"\n{sum(len(r['findings']) for r in results)} findings across {len(results)} areas, ${total:.2f}")
    if a.dry_run:
        for r in results:
            print(f"\n## {r['area']}: {r['summary']}")
            for f in r["findings"]:
                print(f"- [{f['severity']}] {f['file']}:{f['line']}: {f['title']}")
        return
    file_issues(cfg, results)


if __name__ == "__main__":
    main()
