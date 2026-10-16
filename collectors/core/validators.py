"""
collectors/core/validators.py — pure validation rules. Each returns None if OK, else a reason.
Drafted by local model ornith:9b; defects found in review (date minus int crash) fixed by Claude.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Optional


def not_future(d: date, today: date) -> Optional[str]:
    return f"period {d} is in the future (today {today})" if d > today else None


def future_period(d: date, today: date, grace_days: int = 20) -> Optional[str]:
    cutoff = today - timedelta(days=grace_days)
    if d > cutoff:
        return f"period {d} ended after cutoff {cutoff} (today {today} minus {grace_days} days) — filing cannot exist yet"
    return None


def after_listing(d: date, listing_date: Optional[date], grace_days: int = 0) -> Optional[str]:
    if listing_date is None:
        return None
    if d < listing_date - timedelta(days=grace_days):
        return f"period {d} before listing {listing_date}"
    return None


def quarter_sequence(new_end: date, latest_end: Optional[date]) -> Optional[str]:
    if latest_end is not None and new_end < latest_end:
        return f"period {new_end} older than stored {latest_end}"
    return None


def pl_arithmetic(sales, expenses, op_profit, tol_pct: float = 2.0) -> Optional[str]:
    if sales is None or expenses is None or op_profit is None:
        return None
    diff = abs((sales - expenses) - op_profit)
    tol = max(1.5, abs(sales) * tol_pct / 100)   # screener rounds to whole crores
    if diff > tol:
        return f"sales {sales:.2f} - expenses {expenses:.2f} != op profit {op_profit:.2f} (off by {diff:.2f})"
    return None


def pbt_chain(op_profit, other_income, interest, depreciation, pbt, tol_pct: float = 3.0) -> Optional[str]:
    if None in (op_profit, other_income, interest, depreciation, pbt):
        return None
    expected = op_profit + other_income - interest - depreciation
    tol = max(2.5, abs(pbt) * tol_pct / 100, abs(op_profit) * tol_pct / 100)   # 4 rounded inputs
    if abs(expected - pbt) > tol:
        return f"PBT {pbt:.2f} != op {op_profit:.2f} + other {other_income:.2f} - int {interest:.2f} - dep {depreciation:.2f} ({expected:.2f})"
    return None


def holding_sum(parts: Dict[str, Optional[float]], tol: float = 0.5) -> Optional[str]:
    vals = [v for v in parts.values() if v is not None]
    if len(vals) < 2:
        return None
    total = sum(vals)
    return f"shareholding adds to {total:.2f}, not 100" if abs(total - 100) > tol else None


def pledge_ok(promoter_pct, pledged_pct_of_total) -> Optional[str]:
    if promoter_pct is None or pledged_pct_of_total is None:
        return None
    if pledged_pct_of_total < 0 or pledged_pct_of_total > promoter_pct + 0.01:
        return f"pledge {pledged_pct_of_total:.2f}% invalid vs promoter {promoter_pct:.2f}%"
    return None


def sudden_jump(old, new, factor: float = 10.0) -> Optional[str]:
    if old is None or new is None or abs(old) < 1:
        return None
    if old > 0 > new and abs(old) >= 100 and abs(new) >= 100:
        return "sign flip on large values"
    if abs(new) > abs(old) * factor:
        return f"jump x{abs(new) / abs(old):.1f}"
    if 1 <= abs(new) < abs(old) / factor:
        return f"drop x{abs(old) / abs(new):.1f}"
    return None


def cross_source(a, b, tol_pct: float = 5.0) -> Optional[str]:
    if a is None or b is None:
        return None
    tol = max(0.5, max(abs(a), abs(b)) * tol_pct / 100)
    return f"sources differ: {a:.2f} vs {b:.2f}" if abs(a - b) > tol else None
