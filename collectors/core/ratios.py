"""collectors/core/ratios.py — our own ratios from validated quarters + our own price (one formula for every stock)."""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 2)


def compute_ratios(quarters: List[Dict[str, Any]], price: Optional[float], shares_cr: Optional[float],
                   equity_cr: Optional[float], borrowings_cr: Optional[float], cash_cr: Optional[float]) -> Dict[str, Any]:
    by_end = {q["period_end"]: q for q in quarters if q.get("period_end")}
    qs = [by_end[k] for k in sorted(by_end)][-8:]
    window = qs[-4:]
    valid = len(window) == 4 and all(70 <= (window[i]["period_end"] - window[i - 1]["period_end"]).days <= 110 for i in range(1, 4))

    def ttm(key: str) -> Optional[float]:
        if not valid:
            return None
        vals = [q.get(key) for q in window]
        return None if any(v is None for v in vals) else sum(vals)

    rev, npf, eps = ttm("revenue_cr"), ttm("net_profit_cr"), ttm("eps")
    mcap = price * shares_cr if price and shares_cr else None
    eq_ok = equity_cr is not None and equity_cr > 0
    pbt, intr, op = ttm("pbt_cr"), ttm("interest_cr"), ttm("op_profit_cr")
    denom = (equity_cr or 0) + (borrowings_cr or 0)

    def yoy(key: str) -> Optional[float]:
        if not qs:
            return None
        last = qs[-1]
        for q in qs[:-1]:
            if 350 <= (last["period_end"] - q["period_end"]).days <= 380:
                old, new = q.get(key), last.get(key)
                if old and old > 0 and new is not None:
                    return (new / old - 1) * 100
        return None

    return {
        "ttm_revenue_cr": _r(rev), "ttm_net_profit_cr": _r(npf), "ttm_eps": _r(eps), "market_cap_cr": _r(mcap),
        "pe": _r(price / eps) if price and eps and eps > 0 else None,
        "pb": _r(mcap / equity_cr) if mcap and eq_ok else None,
        "roe_pct": _r(npf / equity_cr * 100) if npf is not None and eq_ok else None,
        "roce_pct": _r((pbt + intr) / denom * 100) if pbt is not None and intr is not None and denom > 0 else None,
        "debt_to_equity": _r(borrowings_cr / equity_cr) if eq_ok and borrowings_cr is not None else None,
        "net_debt_cr": _r(borrowings_cr - cash_cr) if borrowings_cr is not None and cash_cr is not None else None,
        "revenue_yoy_pct": _r(yoy("revenue_cr")), "profit_yoy_pct": _r(yoy("net_profit_cr")),
        "opm_ttm_pct": _r(op / rev * 100) if op is not None and rev and rev > 0 else None,
        "quarters_used": len(window) if valid else 0,
        "as_of": qs[-1]["period_end"].isoformat() if qs else None,
    }
