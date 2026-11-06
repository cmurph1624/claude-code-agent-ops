"""Map a call to its usage source using tags first, then the ordered rules in sources.toml."""

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache

from usage.paths import HERE, HOME


@dataclass(frozen=True)
class Rule:
    cwd: re.Pattern[str]
    entrypoint: str | None
    source: str


@lru_cache
def rules() -> tuple[Rule, ...]:
    with (HERE / "sources.toml").open("rb") as fh:
        raw = tomllib.load(fh)["rule"]
    return tuple(Rule(re.compile(r["cwd"]), r.get("entrypoint"), r["source"]) for r in raw)


def tilde(cwd: str | None) -> str:
    cwd = cwd or ""
    home = str(HOME)
    return "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd


def source_of(cwd: str | None, entrypoint: str | None, tag: str | None = None) -> str:
    if tag:
        return tag
    path = tilde(cwd)
    for r in rules():
        if r.entrypoint and r.entrypoint != entrypoint:
            continue
        m = r.cwd.search(path)
        if m:
            return r.source.format(*([m.group(0)] + list(m.groups())))
    return "other:unknown"


def kind(source: str) -> str:
    """interactive vs headless, for the top-line split."""
    return "interactive" if source.startswith("interactive:") else "headless"
