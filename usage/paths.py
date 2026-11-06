import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state"))
    d = base / "usage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_dir() -> Path:
    return Path(os.environ.get("USAGE_PROJECTS_DIR", HOME / ".claude/projects"))
