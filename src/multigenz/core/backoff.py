"""Capped exponential backoff with jitter (rate-limit handling)."""

from __future__ import annotations

import random


def backoff_delay(retry: int, *, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Delay before retry number ``retry`` (0-based): min(cap, base*2^retry) * U(0.5, 1.0)."""
    r = rng or random
    return min(cap, base * (2 ** max(0, retry))) * r.uniform(0.5, 1.0)
