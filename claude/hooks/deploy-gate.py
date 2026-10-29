#!/usr/bin/env python3
"""PreToolUse hook (Bash): deploy-shaped commands always get a permission prompt, in every session
and every permission mode, including the ops agent. A user's go-ahead in chat is not enough; they
approve the actual command.

Gated:
  - deploy scripts: install*.sh, deploy.sh, setup-server.sh
  - systemctl start/stop/restart/reload/enable/disable of a deployed unit
  - gh workflow run (all deploy workflows are dispatch-triggered)
  - docker compose up/down/restart/stop, docker restart/stop/rm
  - gh pr merge in repos where a merge auto-deploys (deployments.yaml `merge_deploys: true`)
  - git checkout/switch/pull/merge/reset/rebase/stash touching a live checkout (`live_checkout: true`),
    because for those trees changing the files IS the deploy
  - Supabase writes: `supabase db push/reset`, `supabase migration up/repair/squash`, functions deploy,
    secrets, branches, storage writes; and every Supabase MCP tool except reads (list_*, get_*,
    search_docs, generate_typescript_types, and execute_sql that is plainly a SELECT).
  - Artifact publishes for `mechanism: artifact` projects: any publish to their
    `artifact_url`, or from a bundle dir (any directory named ops-<name>, including a session
    scratchpad copy) or their ~/dev checkout.

Fails open (no output) on any error, so a broken gate never blocks normal work.
"""
import json
import os
import re
import shlex
import sys
from pathlib import Path

QUICK = re.compile(r"install[\w-]*\.sh|deploy\.sh|setup-server\.sh|systemctl|gh\s+workflow\s+run|docker"
                   r"|gh\s+pr\s+merge|\bgit\b|supabase")
# systemd unit names of your deployed services; edit to match yours.
UNITS = r"(example-daemon|example-worker[\w@.-]*)"
RULES = [
    (re.compile(r"(^|[\s/;&|(])(install[\w-]*|deploy|setup-server)\.sh\b"), "runs a deploy/install script"),
    (re.compile(rf"systemctl\s+(--user\s+)?(--\S+\s+)*(start|stop|restart|reload|enable|disable|try-restart)\b[^;&|]*\b{UNITS}"),
     "starts/stops/restarts a deployed service"),
    (re.compile(r"\bgh\s+workflow\s+run\b"), "dispatches a GitHub Actions workflow (deploys are dispatch-triggered)"),
    (re.compile(r"\bsupabase\s+db\s+push\b"),
     "pushes EVERY local migration not in the remote history table. If any were applied by hand, this "
     "re-runs them on the remote database. Use apply_migration for one file"),
    (re.compile(r"\bsupabase\s+(db\s+(reset|execute|query)|migration\s+(up|repair|squash|down)|functions\s+(deploy|delete)"
                r"|secrets\s+(set|unset)|branches\s+(create|delete|update)|storage\s+(rm|cp|mv)|projects\s+delete)\b"),
     "writes to the Supabase project"),
    (re.compile(r"\bdocker\s+compose\b[^;&|]*\b(up|down|restart|stop)\b|\bdocker\s+(restart|stop|rm)\b"),
     "changes running containers"),
]
GIT_MUTATE = re.compile(r"\bgit\s+(-C\s+\S+\s+)?(checkout|switch|pull|merge|reset|rebase|stash)\b")


def registry():
    import yaml
    reg = yaml.safe_load((Path(__file__).resolve().parents[2] / "ops/deployments.yaml").read_text())["projects"]
    dev = Path.home() / "dev"
    live = {(dev / p["dir"]).resolve() for p in reg.values() if p.get("live_checkout")}
    merge_deploys = {(dev / p["dir"]).resolve() for p in reg.values() if p.get("merge_deploys")}
    return live, merge_deploys


def dirs_touched(cmd, cwd):
    """cwd plus every `cd X` / `git -C X` target in the command, resolved."""
    out, here = [Path(cwd)], Path(cwd)
    try:
        toks = shlex.split(cmd.replace(";", " ; ").replace("&&", " && "), posix=True)
    except ValueError:
        toks = cmd.split()
    for i, t in enumerate(toks[:-1]):
        if t in ("cd", "-C", "pushd"):
            p = Path(os.path.expanduser(toks[i + 1]))
            here = p if p.is_absolute() else here / p
            out.append(here)
    return {d.resolve() for d in out}


def inside(d, roots):
    return any(d == r or r in d.parents for r in roots)


def reason(cmd, cwd):
    for rx, why in RULES:
        if rx.search(cmd):
            return why
    if re.search(r"\bgh\s+pr\s+merge\b", cmd) or GIT_MUTATE.search(cmd):
        live, merge_deploys = registry()
        touched = dirs_touched(cmd, cwd)
        if re.search(r"\bgh\s+pr\s+merge\b", cmd) and any(inside(d, merge_deploys) for d in touched):
            return "merges a PR in a repo where merging auto-deploys"
        if GIT_MUTATE.search(cmd) and any(inside(d, live) for d in touched):
            return "changes files in a live checkout, so this IS a deploy of that service"
    return None


def artifact_reason(inp):
    """Why an Artifact tool call publishes a deployed artifact project, or None."""
    if inp.get("action", "publish") != "publish":
        return None
    import yaml
    reg = yaml.safe_load((Path(__file__).resolve().parents[2] / "ops/deployments.yaml").read_text())["projects"]
    dev = Path.home() / "dev"
    paths = [inp.get(k) for k in ("file_path", "root")] + list(inp.get("file_paths") or [])
    paths = [Path(os.path.expanduser(x)).resolve() for x in paths if x]
    for name, p in reg.items():
        if p.get("mechanism") != "artifact":
            continue
        roots = {(dev / p["dir"]).resolve()}
        bundle = any(f"ops-{name}" in x.parts for x in paths)   # /tmp/claude-1000/ops-x or <scratchpad>/ops-x
        if (p.get("artifact_url") and inp.get("url") == p["artifact_url"]) or bundle or any(inside(x, roots) for x in paths):
            return f"publishes {name} to its artifact link, which is what players open"
    return None


SUPABASE_READ = re.compile(r"^(list_|get_|search_docs$|generate_typescript_types$)")
SQL_WRITE = re.compile(r"\b(insert|update|delete|merge|alter|drop|create|truncate|grant|revoke|comment|copy|call|do"
                       r"|vacuum|refresh|reindex|cluster|lock|set|reset|notify|security|into)\b", re.I)


def supabase_mcp_reason(tool, inp):
    name = tool.split("__", 2)[-1]
    if SUPABASE_READ.match(name):
        return None
    if name == "execute_sql":
        q = inp.get("query", "")
        if re.match(r"\s*(select|explain|show)\b", q, re.I) and not SQL_WRITE.search(q) and q.count(";") <= 1:
            return None
        return "runs SQL that may write to the Supabase project"
    if name == "apply_migration":
        return f"applies migration `{inp.get('name', '?')}` to the Supabase project"
    return f"calls Supabase `{name}`, which changes the project"


def main():
    ev = json.load(sys.stdin)
    tool = ev.get("tool_name", "")
    if tool == "Artifact":
        why = artifact_reason(ev.get("tool_input", {}))
        if why:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": f"Deploy gate: this {why}. Deploys need your explicit OK "
                                            f"(~/dev/agent-ops/claude/hooks/deploy-gate.py)."}}))
        return
    if tool.startswith("mcp__"):
        why = supabase_mcp_reason(tool, ev.get("tool_input", {})) if "supabase" in tool.lower() else None
        if why:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "ask",
                "permissionDecisionReason": f"Supabase gate: this {why}. Needs your explicit OK "
                                            f"(~/dev/agent-ops/claude/hooks/deploy-gate.py)."}}))
        return
    cmd = ev.get("tool_input", {}).get("command", "")
    if not QUICK.search(cmd):
        return
    why = reason(cmd, ev.get("cwd") or os.getcwd())
    if why:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": f"Deploy gate: this command {why}. Deploys need your explicit OK "
                                        f"(~/dev/agent-ops/claude/hooks/deploy-gate.py).",
        }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
