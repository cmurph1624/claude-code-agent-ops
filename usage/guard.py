"""One call for anything that spends tokens on its own: `paused()` says whether to hold off.

Fails open: if the gate can't be read or computed, the answer is "not paused".
"""

from usage import gate


def paused() -> str | None:
    """The reason to pause (gate is `flag`, no override), or None to go ahead."""
    try:
        g = gate.refresh()
    except Exception:  # noqa: BLE001
        return None
    return g.reason if g.state == "flag" else None
