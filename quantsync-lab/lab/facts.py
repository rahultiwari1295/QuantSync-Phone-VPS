"""Company, ownership, delivery and event fields for the scanner, joined point-in-time onto each symbol's bars.

Sources (all optional; a missing source just leaves its fields empty, and a rule on an empty field never matches):
  intel.db        (QS_INTEL_DB)  facts_daily, facts_owner, insider_trades, deals, events, meetings  -- written by intel/
  stock_history   (QS_HIST_DB)   nse_delivery_history                                              -- written by the engine
Every value is keyed by the day it became PUBLIC (`known_at`), never the period it describes, so backtests cannot
see results, holdings or deals before the market did."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

F = lambda key, label, group, kind="number", unit="": {"key": key, "label": label, "group": group, "kind": kind, "unit": unit}

BUSINESS = [
    F("mcap_cr", "Market cap", "Business", unit="₹ cr"), F("pe", "P/E", "Business"), F("pb", "Price / book", "Business"),
    F("earnings_yield", "Earnings yield", "Business", unit="%"), F("dividend_yield", "Dividend yield", "Business", unit="%"),
    F("roe", "Return on equity", "Business", unit="%"), F("roce", "Return on capital", "Business", unit="%"),
    F("debt_to_equity", "Debt / equity", "Business"), F("opm", "Operating margin (TTM)", "Business", unit="%"),
    F("eps_ttm", "EPS (TTM)", "Business", unit="₹"), F("sales_ttm_cr", "Sales (TTM)", "Business", unit="₹ cr"),
    F("profit_ttm_cr", "Profit (TTM)", "Business", unit="₹ cr"),
    F("sales_growth_yoy", "Sales growth (YoY)", "Growth", unit="%"), F("profit_growth_yoy", "Profit growth (YoY)", "Growth", unit="%"),
    F("sales_growth_3y", "Sales growth 3y (CAGR)", "Growth", unit="%"), F("profit_growth_3y", "Profit growth 3y (CAGR)", "Growth", unit="%"),
    F("sales_growth_5y", "Sales growth 5y (CAGR)", "Growth", unit="%"), F("profit_growth_5y", "Profit growth 5y (CAGR)", "Growth", unit="%"),
    F("sales_growth_10y", "Sales growth 10y (CAGR)", "Growth", unit="%"), F("profit_growth_10y", "Profit growth 10y (CAGR)", "Growth", unit="%"),
    F("roe_3y", "Return on equity, 3y avg", "Business", unit="%"),
]
OWNERS = [
    F("promoter_pct", "Promoter holding", "Owners", unit="%"), F("promoter_change_qoq", "Promoter change (QoQ)", "Owners", unit="pts"),
    F("pledged_pct", "Promoter shares pledged", "Owners", unit="%"), F("pledge_change_qoq", "Pledge change (QoQ)", "Owners", unit="pts"),
    F("fii_pct", "FII holding", "Owners", unit="%"), F("fii_change_qoq", "FII change (QoQ)", "Owners", unit="pts"),
    F("dii_pct", "DII holding", "Owners", unit="%"), F("dii_change_qoq", "DII change (QoQ)", "Owners", unit="pts"),
    F("public_pct", "Public holding", "Owners", unit="%"),
]
MARKET = [
    F("delivery_pct", "Delivery %", "Volume", unit="%"), F("delivery_ratio", "Delivery qty vs 20-day avg", "Volume", unit="x"),
]
EVENTS = [
    F("insider_buy_cr_30d", "Insider buying, 30 days", "Events", unit="₹ cr"), F("insider_sell_cr_30d", "Insider selling, 30 days", "Events", unit="₹ cr"),
    F("promoter_buy_cr_90d", "Promoter open-market buying, 90 days", "Events", unit="₹ cr"),
    F("insider_buyers_90d", "Distinct insiders buying, 90 days", "Events"),
    F("bulk_buy_count_10d", "Bulk/block buys, 10 days", "Events"), F("bulk_sell_count_10d", "Bulk/block sells, 10 days", "Events"),
    F("events_7d", "Filings and news, 7 days", "Events"), F("impact_max_7d", "Highest impact score, 7 days", "Events"),
    F("negative_events_30d", "Negative events, 30 days", "Events"),
    F("order_wins_90d", "Order wins, 90 days", "Events"), F("days_since_order_win", "Days since last order win", "Events", unit="days"),
    F("days_since_results", "Days since last results", "Events", unit="days"),
    F("rating_upgrades_180d", "Credit rating upgrades, 180 days", "Events"), F("rating_downgrades_180d", "Credit rating downgrades, 180 days", "Events"),
    F("fund_raise_90d", "Fund-raise announcements, 90 days", "Events"),
    F("days_to_board_meeting", "Days to next board meeting", "Events", unit="days"), F("days_to_results", "Days to next results meeting", "Events", unit="days"),
]
FACT_FEATURES = BUSINESS + OWNERS + MARKET + EVENTS
FACT_KEYS = [f["key"] for f in FACT_FEATURES]
_DAILY = [f["key"] for f in BUSINESS]
_OWNER = [f["key"] for f in OWNERS]
DAY = np.timedelta64(1, "D")


def _ro(path: str | None):
    if not path or not Path(path).exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def _has(con, table: str) -> bool:
    return bool(con and con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone())


class Facts:
    """Open once per cook; enrich(symbol, frame) adds every FACT_KEYS column (NaN where unknown)."""

    def __init__(self, cfg=None):
        extra = getattr(cfg, "extra", {}) or {}
        data = os.environ.get("QS_DATA_DIR", "/root/quantsync/Data")
        self.intel = _ro(os.environ.get("QS_INTEL_DB") or extra.get("intel_db") or os.path.join(data, "intel.db"))
        self.hist = _ro(os.environ.get("QS_HIST_DB") or extra.get("hist_db") or os.path.join(data, "stock_history.db"))
        t = lambda n: _has(self.intel, n)
        self.has = {n: t(n) for n in ("facts_daily", "facts_owner", "insider_trades", "deals", "events", "meetings")}
        self.has["delivery"] = _has(self.hist, "nse_delivery_history")

    @property
    def available(self) -> dict:
        return dict(self.has)

    def close(self):
        for c in (self.intel, self.hist):
            if c:
                c.close()

    # -- helpers -----------------------------------------------------------------------------------
    def _q(self, con, sql, args) -> pd.DataFrame:
        try:
            return pd.read_sql_query(sql, con, params=args)
        except Exception:  # noqa: BLE001 - a bad source must never break the cook
            return pd.DataFrame()

    @staticmethod
    def _asof(f: pd.DataFrame, src: pd.DataFrame, cols: list[str]) -> None:
        if src.empty:
            return
        src = src.copy()
        src["date"] = pd.to_datetime(src["known_at"]).astype("datetime64[ns]")
        src = src.sort_values("date").drop_duplicates("date", keep="last")
        left = pd.DataFrame({"date": pd.to_datetime(f["date"]).astype("datetime64[ns]")})
        m = pd.merge_asof(left, src[["date"] + [c for c in cols if c in src.columns]], on="date", direction="backward")
        for c in cols:
            if c in m.columns:
                f[c] = pd.to_numeric(m[c], errors="coerce").to_numpy()

    @staticmethod
    def _window(dates: np.ndarray, ev_d: np.ndarray, ev_v: np.ndarray, days: int) -> np.ndarray:
        """Sum of event values with known date in (bar_date - days, bar_date]."""
        if not len(ev_d):
            return np.zeros(len(dates))
        o = np.argsort(ev_d); ev_d, ev_v = ev_d[o], ev_v[o]
        cs = np.concatenate([[0.0], np.cumsum(ev_v)])
        hi = np.searchsorted(ev_d, dates, side="right")
        lo = np.searchsorted(ev_d, dates - days * DAY, side="right")
        return cs[hi] - cs[lo]

    @staticmethod
    def _since(dates: np.ndarray, ev_d: np.ndarray) -> np.ndarray:
        out = np.full(len(dates), np.nan)
        if not len(ev_d):
            return out
        ev_d = np.sort(ev_d)
        hi = np.searchsorted(ev_d, dates, side="right")
        ok = hi > 0
        out[ok] = (dates[ok] - ev_d[hi[ok] - 1]) / DAY
        return out

    @staticmethod
    def _until(dates: np.ndarray, rows: pd.DataFrame) -> np.ndarray:
        """Days to the next meeting already announced on each bar date."""
        out = np.full(len(dates), np.nan)
        for kn, md in zip(pd.to_datetime(rows["known_at"]).to_numpy(), pd.to_datetime(rows["meeting_date"]).to_numpy()):
            m = (dates >= kn) & (dates <= md)
            d = (md - dates[m]) / DAY
            out[m] = np.fmin(out[m], d)
        return out

    # -- main ----------------------------------------------------------------------------------------
    def enrich(self, sym: str, f: pd.DataFrame) -> pd.DataFrame:
        f = pd.concat([f, pd.DataFrame(np.nan, index=f.index, columns=[k for k in FACT_KEYS if k not in f.columns])], axis=1)
        dates = pd.to_datetime(f["date"]).to_numpy().astype("datetime64[ns]")
        if self.has["facts_daily"]:
            self._asof(f, self._q(self.intel, "SELECT * FROM facts_daily WHERE symbol=? ORDER BY known_at", (sym,)), _DAILY)
        if self.has["facts_owner"]:
            self._asof(f, self._q(self.intel, "SELECT * FROM facts_owner WHERE symbol=? ORDER BY known_at", (sym,)), _OWNER)
        if self.has["delivery"]:
            d = self._q(self.hist, "SELECT trade_date, deliv_pct, deliv_qty FROM nse_delivery_history WHERE symbol=? AND series='EQ' ORDER BY trade_date", (sym,))
            if not d.empty:
                d["date"] = pd.to_datetime(d["trade_date"], errors="coerce")
                d = d.dropna(subset=["date"]).drop_duplicates("date", keep="last")
                d["delivery_ratio"] = d["deliv_qty"] / d["deliv_qty"].rolling(20, min_periods=10).mean().shift(1)
                m = pd.DataFrame({"date": pd.to_datetime(f["date"])}).merge(d[["date", "deliv_pct", "delivery_ratio"]], on="date", how="left")
                f["delivery_pct"] = m["deliv_pct"].to_numpy()
                f["delivery_ratio"] = m["delivery_ratio"].to_numpy()
        if self.has["insider_trades"]:
            t = self._q(self.intel, "SELECT known_at, person, category, side, value_cr, mode FROM insider_trades WHERE symbol=?", (sym,))
            if not t.empty:
                kd = pd.to_datetime(t["known_at"], errors="coerce").to_numpy().astype("datetime64[ns]")
                v = pd.to_numeric(t["value_cr"], errors="coerce").fillna(0).to_numpy()
                buy = (t["side"].str.lower() == "buy").to_numpy()
                market = t["mode"].fillna("").str.lower().str.contains("market").to_numpy()
                prom = t["category"].fillna("").str.lower().str.contains("promoter").to_numpy()
                f["insider_buy_cr_30d"] = self._window(dates, kd[buy], v[buy], 30)
                f["insider_sell_cr_30d"] = self._window(dates, kd[~buy], v[~buy], 30)
                pm = buy & market & prom
                f["promoter_buy_cr_90d"] = self._window(dates, kd[pm], v[pm], 90)
                # distinct buyers: count first buy per person inside the window (approximation: buys counted once per person per day)
                bt = t[buy].assign(k=kd[buy]).drop_duplicates(["person", "k"])
                f["insider_buyers_90d"] = self._window(dates, bt["k"].to_numpy().astype("datetime64[ns]"), np.ones(len(bt)), 90)
        if self.has["deals"]:
            t = self._q(self.intel, "SELECT known_at, side FROM deals WHERE symbol=?", (sym,))
            if not t.empty:
                kd = pd.to_datetime(t["known_at"], errors="coerce").to_numpy().astype("datetime64[ns]")
                buy = (t["side"].str.lower() == "buy").to_numpy()
                f["bulk_buy_count_10d"] = self._window(dates, kd[buy], np.ones(buy.sum()), 10)
                f["bulk_sell_count_10d"] = self._window(dates, kd[~buy], np.ones((~buy).sum()), 10)
        if self.has["events"]:
            t = self._q(self.intel, "SELECT known_at, type, sentiment, impact FROM events WHERE symbol=?", (sym,))
            if not t.empty:
                kd = pd.to_datetime(t["known_at"], errors="coerce").to_numpy().astype("datetime64[ns]")
                ty = t["type"].fillna("").to_numpy()
                one = lambda m: (kd[m], np.ones(int(m.sum())))
                f["events_7d"] = self._window(dates, kd, np.ones(len(t)), 7)
                neg = (t["sentiment"] == "negative").to_numpy()
                f["negative_events_30d"] = self._window(dates, *one(neg), 30)
                ow = ty == "order_win"
                f["order_wins_90d"] = self._window(dates, *one(ow), 90)
                f["days_since_order_win"] = self._since(dates, kd[ow])
                f["days_since_results"] = self._since(dates, kd[ty == "results"])
                f["rating_upgrades_180d"] = self._window(dates, *one(ty == "rating_upgrade"), 180)
                f["rating_downgrades_180d"] = self._window(dates, *one(ty == "rating_downgrade"), 180)
                f["fund_raise_90d"] = self._window(dates, *one(ty == "fund_raise"), 90)
                imp = pd.to_numeric(t["impact"], errors="coerce").fillna(0).to_numpy()
                # rolling max over 7 days: evaluate per bar (events per symbol are few)
                o = np.argsort(kd); kd2, imp2 = kd[o], imp[o]
                hi = np.searchsorted(kd2, dates, side="right"); lo = np.searchsorted(kd2, dates - 7 * DAY, side="right")
                f["impact_max_7d"] = [float(imp2[a:b].max()) if b > a else 0.0 for a, b in zip(lo, hi)]
        if self.has["meetings"]:
            t = self._q(self.intel, "SELECT known_at, meeting_date, is_results FROM meetings WHERE symbol=?", (sym,))
            if not t.empty:
                t = t.dropna(subset=["known_at", "meeting_date"])
                f["days_to_board_meeting"] = self._until(dates, t)
                f["days_to_results"] = self._until(dates, t[t["is_results"].fillna(0).astype(int) == 1])
        return f
