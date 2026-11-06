"""API-equivalent cost of a call from prices.toml."""

import tomllib
from functools import lru_cache
from typing import Any

from usage.paths import HERE

PER_TOKEN = 1 / 1_000_000


@lru_cache
def prices() -> dict[str, Any]:
    with (HERE / "prices.toml").open("rb") as fh:
        return tomllib.load(fh)


def cost(model: str | None, input: int, output: int, cache_read: int, cache_5m: int,
         cache_1h: int) -> float | None:
    """USD at list price, or None when the model isn't in the table."""
    p = prices()["models"].get(model or "")
    if p is None:
        return None
    prompt = input + cache_read + cache_5m + cache_1h
    scale = p.get("long_scale", 1.0) if prompt > p.get("long_context_threshold", 1 << 62) else 1.0
    return PER_TOKEN * scale * (
        input * p["input"] + output * p["output"] + cache_read * p["cache_read"]
        + cache_5m * p["cache_write"] + cache_1h * p["cache_write_1h"]
    )
