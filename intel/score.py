"""Impact score 0..100: how much an event is likely to matter for its stock.

  base (by type)  + size vs company (deal or order value / market cap)  + surprise (rare for this company)
  + who (promoter, top fund)  - already priced (big move before we saw it)
The parts are returned too, so the UI can say WHY something scored high."""
from __future__ import annotations

import math


def impact(base: int, value_cr: float | None = None, mcap_cr: float | None = None, first_in_year: bool = False,
           promoter: bool = False, prior_move_pct: float | None = None, watch: bool = False) -> tuple[int, dict]:
    parts = {"base": base}
    if value_cr and mcap_cr and mcap_cr > 0:
        r = value_cr / mcap_cr * 100  # % of market value
        parts["size"] = int(min(25, round(8 * math.log10(1 + r * 4))))
    elif value_cr:
        parts["size"] = int(min(12, round(4 * math.log10(1 + value_cr))))
    if first_in_year:
        parts["surprise"] = 8
    if promoter:
        parts["who"] = 8
    if prior_move_pct is not None and abs(prior_move_pct) > 8:
        parts["priced_in"] = -int(min(15, abs(prior_move_pct) - 8))
    if watch:
        parts["watchlist"] = 5
    return max(0, min(100, sum(parts.values()))), parts
