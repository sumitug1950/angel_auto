"""IV Rank from India VIX - a market-wide volatility gauge, not tied to any single option's
back-solved IV (which is noisier). See iv_solver.py for the per-option IV used in strike
selection instead.

No DB dependency here - the historical window is fetched by the caller (from the IVHistory
table, once persistence/ exists in Phase 3) and passed in as plain values, so this stays
fully unit-testable on its own.
"""
from __future__ import annotations


def compute_iv_rank(current_vix: float, historical_vix: list[float]) -> float:
    """IV Rank = (current - min) / (max - min) * 100 over the given historical window.

    Raises ValueError if there's no usable history yet (empty window, or zero range) -
    callers should treat that as "not enough data" and fall back to the DEBIT structure
    default rather than trusting a meaningless rank.
    """
    if not historical_vix:
        raise ValueError("historical_vix is empty - not enough data to compute IV Rank yet")

    vix_min = min(historical_vix)
    vix_max = max(historical_vix)
    if vix_max == vix_min:
        raise ValueError("historical_vix has zero range - cannot compute a meaningful IV Rank")

    rank = (current_vix - vix_min) / (vix_max - vix_min) * 100.0
    return max(0.0, min(100.0, rank))
