#!/usr/bin/env python3
"""Read-only deploy status for every project in deployments.yaml: deployed ref vs origin, drift,
dirty live checkouts and health. Then failed systemd units and unhealthy containers.

    status.py            # table
    status.py --json     # machine-readable, used by the ops agent
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

DEV = Path.home() / "dev"
REG = yaml.safe_load((Path(__file__).resolve().parent / "deployments.yaml").read_text())["projects"]
ENV = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}


def sh(cmd, cwd=None, timeout=20):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=ENV)
        return p.returncode, p.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def project(name, p):
    r = {"project": name, "mechanism": p.get("mechanism")}
    if p.get("mechanism") == "none":
        return r
    d = DEV / p["dir"]
    if not d.is_dir():
        r.update(checkout="-", head="-", origin="-", drift=[f"no checkout at {d}"])
        return r
    _, head = sh(["git", "rev-parse", "HEAD"], d)
    _, branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], d)
    _, remote = sh(["git", "ls-remote", "origin", f"refs/heads/{p['branch']}"], d)
    remote = remote.split()[0] if remote else ""
    _, dirty = sh(["git", "status", "--porcelain", "--untracked-files=no"], d)
    behind = ""
    if remote and head != remote:
        rc, cnt = sh(["git", "rev-list", "--count", f"{head}..{remote}"], d)
        behind = (cnt if cnt != "0" else "") if rc == 0 else "?"  # "?" = remote commit not fetched locally
    r.update(checkout=branch if branch != "HEAD" else "(detached)", head=head[:8], origin=remote[:8],
             tracks=p["branch"], behind=behind, dirty=bool(dirty))
    if p["mechanism"] in ("compose", "gha", "vercel") or not p.get("live_checkout"):
        r["note"] = "running build may differ from checkout"
    if p.get("health"):
        rc, _ = sh(p["health"], d)
        r["healthy"] = rc == 0
    drift = []
    if branch not in ("HEAD", p["branch"]):
        drift.append(f"on branch {branch}, not {p['branch']}")
    if behind:
        drift.append(f"{behind} behind origin/{p['branch']}")
    if dirty:
        drift.append("uncommitted changes")
    if p["mechanism"] == "artifact":
        pub = last_published(name)
        r["published"] = pub or "?"
        if not pub:
            drift.append("no publish recorded in deploy.log")
        elif remote and not remote.startswith(pub):
            rc, cnt = sh(["git", "rev-list", "--count", f"{pub}..{remote}"], d)
            drift.append(f"{cnt if rc == 0 else '?'} commit(s) on origin/{p['branch']} not published")
    r["drift"] = drift
    return r


def last_published(name):
    """sha8 of the last good deploy of `name` in the ops deploy log (artifact projects have no other record)."""
    log = Path.home() / ".local/state/ops/deploy.log"
    sha = None
    for line in (log.read_text().splitlines() if log.exists() else []):
        f = line.split()
        if len(f) >= 4 and f[1] == name and f[3] == "ok" and "->" in f[2]:
            sha = f[2].split("->")[1]
    return sha


def system():
    out = {}
    _, out["failed_system_units"] = sh("systemctl --failed --no-legend --plain | awk '{print $1}'")
    _, out["failed_user_units"] = sh("systemctl --user --failed --no-legend --plain | awk '{print $1}'")
    _, out["unhealthy_containers"] = sh("docker ps -a --filter health=unhealthy --format '{{.Names}}'; "
                                        "docker ps -a --filter status=exited --filter status=restarting --format '{{.Names}} ({{.Status}})'")
    return {k: [x for x in v.splitlines() if x] for k, v in out.items()}


def main():
    rows = [project(n, p) for n, p in REG.items()]
    sysinfo = system()
    if "--json" in sys.argv:
        print(json.dumps({"projects": rows, **sysinfo}, indent=1))
        return
    print(f"{'project':18} {'health':7} {'checkout':14} {'head':9} {'origin':9} drift")
    for r in rows:
        if r["mechanism"] == "none":
            continue
        h = {True: "ok", False: "DOWN"}.get(r.get("healthy"), "-")
        print(f"{r['project']:18} {h:7} {r['checkout'][:14]:14} {r['head']:9} {r['origin']:9} "
              f"{'; '.join(r['drift']) or '-'}")
    for k, v in sysinfo.items():
        print(f"{k}: {', '.join(v) or 'none'}")


if __name__ == "__main__":
    main()
