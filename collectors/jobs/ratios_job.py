"""ratios — our own P/E, P/B, ROE, ROCE, D/E, growth from validated quarters + our price."""
from __future__ import annotations

from datetime import date

from ..core import db
from ..core.pipeline import JobRun, listing_dates
from ..core.ratios import compute_ratios
from ._common import hist_conn, universe

NAME = "ratios"
_COLS = ("ttm_revenue_cr", "ttm_net_profit_cr", "ttm_eps", "market_cap_cr", "pe", "pb", "roe_pct", "roce_pct",
         "debt_to_equity", "net_debt_cr", "revenue_yoy_pct", "profit_yoy_pct", "opm_ttm_pct", "quarters_used", "as_of")


def run(run: JobRun, fetcher=None, **kw) -> None:
    with db.session() as conn, hist_conn() as hist:
        fv = {r["symbol"]: r["face_value"] for r in conn.execute("SELECT symbol, face_value FROM listing")}
        px = {r[0]: r[1] for r in hist.execute("SELECT symbol, cmp FROM StockTechnicalsDaily")}
        for sym in universe(hist):
            run.seen += 1
            qs = [{"period_end": date.fromisoformat(r["period_end_date"]), "revenue_cr": r["revenue_cr"], "op_profit_cr": r["op_profit_cr"],
                   "net_profit_cr": r["net_profit_cr"], "eps": r["eps"], "pbt_cr": None, "interest_cr": r["interest_expense_cr"]}
                  for r in hist.execute("SELECT * FROM CompanyFundamentalsHistory WHERE symbol=? AND period_type='QUARTERLY' ORDER BY period_end_date DESC LIMIT 8", (sym,))]
            bs = hist.execute("SELECT equity_capital_cr, reserves_cr, borrowings_cr, cash_cr, roce_pct FROM CompanyFundamentalsHistory "
                              "WHERE symbol=? AND equity_capital_cr IS NOT NULL AND reserves_cr IS NOT NULL ORDER BY period_end_date DESC LIMIT 1", (sym,)).fetchone()
            if not qs:
                continue
            eq = (bs["equity_capital_cr"] + bs["reserves_cr"]) if bs else None
            shares_cr = (bs["equity_capital_cr"] / fv[sym]) if bs and fv.get(sym) else None
            r = compute_ratios(qs, px.get(sym), shares_cr, eq, bs["borrowings_cr"] if bs else None, bs["cash_cr"] if bs else None)
            conn.execute(f"INSERT OR REPLACE INTO ratios_daily(symbol,computed_at,price,{','.join(_COLS)}) VALUES (?,?,?,{','.join('?' * len(_COLS))})",
                         (sym, db.now_iso(), px.get(sym), *[r[c] for c in _COLS]))
            run.saved += 1
        conn.commit()
