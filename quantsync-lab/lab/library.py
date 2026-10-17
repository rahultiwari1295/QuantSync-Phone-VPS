"""The public strategy library: plain rule JSON, backtested on real candles (cached), tracked live like any strategy.

`python -m lab.cli library` creates/updates every entry and runs its past test. Safe to re-run: a strategy whose rules
did not change keeps its id and history; changed rules start a new version. Each entry needs only fields that exist in
the feature store; entries that use company/owner/event fields simply show fewer trades until that data has history."""
from __future__ import annotations

import json

from . import service
from .db import jdump
from .rules import normalise, normalise_exit, rule_hash

c = lambda f, op, v: {"l": {"f": f}, "op": op, "r": {"c": v}}
ff = lambda f, op, g: {"l": {"f": f}, "op": op, "r": {"f": g}}
sig = lambda f: {"l": {"f": f}, "op": "is_true"}
btw = lambda f, a, b: {"l": {"f": f}, "op": "between", "r": {"list": [a, b]}}
ALL = lambda *x: {"v": 1, "all": list(x)}
ANY = lambda *x: {"v": 1, "any": list(x)}
EXIT = lambda stop, target, days, trail=None: {"v": 1, "any": [{"type": "stop_pct", "v": stop}, {"type": "target_pct", "v": target},
                                                                *([{"type": "trail_atr", "v": trail}] if trail else []), {"type": "time", "v": days}]}

# id -> (name, one plain line, family, tf, entry, exit)
LIBRARY: dict[str, tuple] = {
    "lib-52wv": ("52-week high on volume", "New one-year highs with real volume behind them.", "Breakout", "1D",
                 ALL(ff("close", ">", "hh_52"), c("vol_ratio", ">", 1.5)), EXIT(7, 15, 30)),
    "lib-ath": ("All-time high momentum", "Within 3% of an all-time high after a strong 3-month run.", "Breakout", "1D",
                ALL(c("dist_ath_pct", ">", -3), c("ret_60", ">", 20), ff("close", ">", "sma_50")), EXIT(8, 20, 40)),
    "lib-20d": ("20-day breakout in an uptrend", "Closes above its 20-day high while above the 200-day average.", "Breakout", "1D",
                ALL(ff("close", ">", "hh_20"), ff("close", ">", "sma_200"), c("vol_ratio", ">", 1.2)), EXIT(6, 12, 20)),
    "lib-squeeze": ("Bollinger squeeze breakout", "Bands were tight, price breaks above the upper band.", "Breakout", "1D",
                    ALL({"l": {"f": "bb_width_pct", "lag": 1}, "op": "<", "r": {"c": 10}}, ff("close", ">", "bb_upper"), ff("close", ">", "sma_50")), EXIT(5, 12, 20)),
    "lib-nr7": ("NR7 in an uptrend", "Quietest day in a week while the trend is up: a coiled spring.", "Breakout", "1D",
                ALL(sig("is_nr7"), ff("close", ">", "sma_50"), c("sma_50_slope", ">", 0)), EXIT(4, 8, 10)),
    "lib-stage2": ("Stage-2 base breakout", "Leaves a long base as the 30-week average turns up.", "Pattern", "1D",
                   ALL(sig("sig_stage2_base")), EXIT(7, 18, 40)),
    "lib-rs": ("RS leader breakouts", "Market leaders breaking out of a quiet range.", "Pattern", "1D",
               ALL(sig("sig_rs_leader")), EXIT(7, 18, 40)),
    "lib-vcp": ("Tightening base (VCP)", "Shrinking swings that break upward.", "Pattern", "1D",
                ALL(sig("sig_apex_base")), EXIT(6, 15, 30)),
    "lib-htf": ("High tight flag", "Rare, fast pattern. Few trades, big swings.", "Pattern", "1D",
                ALL(sig("sig_high_tight_flag")), EXIT(8, 25, 30)),
    "lib-pgap": ("Power gap", "Big gap up on very heavy volume that held.", "Pattern", "1D",
                 ALL(sig("sig_power_gap")), EXIT(6, 15, 25)),
    "lib-golden": ("Golden cross", "The classic long-term trend signal.", "Trend", "1D",
                   ALL(ff("sma_50", "crosses_above", "sma_200")), EXIT(8, 20, 60)),
    "lib-ema": ("EMA 9 / 21 in an uptrend", "Short-term turn up while the long trend is up.", "Trend", "1D",
                ALL(ff("ema_9", "crosses_above", "ema_21"), ff("close", ">", "sma_200")), EXIT(5, 10, 15)),
    "lib-macd": ("MACD cross in an uptrend", "Momentum turning up inside an uptrend.", "Trend", "1D",
                 ALL(ff("macd", "crosses_above", "macd_signal"), ff("close", ">", "sma_200"), c("adx_14", ">", 18)), EXIT(5, 10, 20)),
    "lib-adx": ("Strong trend, fresh push", "ADX above 25 and RSI crossing 60.", "Trend", "1D",
                ALL(c("adx_14", ">", 25), c("rsi_14", "crosses_above", 60), ff("pdi_14", ">", "ndi_14")), EXIT(6, 12, 20)),
    "lib-weekly": ("Weekly trend start", "Weekly EMA 10 crosses above EMA 20.", "Trend", "1W",
                   ALL(ff("ema_10", "crosses_above", "ema_20"), ff("close", ">", "sma_50")), EXIT(10, 25, 26)),
    "lib-pb": ("Pullback in an uptrend", "Strong stocks that cooled off, studied from the dip.", "Pullback", "1D",
               ALL(ff("close", ">", "sma_200"), btw("rsi_14", 38, 48), c("sma_200_slope", ">", 0)), EXIT(6, 12, 20)),
    "lib-ema20": ("Bounce off EMA 20", "Uptrend touches its 20-day EMA and closes green.", "Pullback", "1D",
                  ALL(btw("dist_ema_20_pct", -1.5, 1.0), sig("is_green"), ff("ema_20", ">", "ema_50"), ff("close", ">", "sma_200")), EXIT(4, 8, 12)),
    "lib-oversold": ("Oversold bounce", "Sharp fall that closes green. Mean-reversion idea.", "Pullback", "1D",
                     ALL(c("rsi_14", "<", 30), sig("is_green"), ff("close", ">", "sma_200")), EXIT(5, 8, 10)),
    "lib-3down": ("Three red days in an uptrend", "Short dip inside a strong trend.", "Pullback", "1D",
                  ALL(c("down_streak", ">=", 3), ff("close", ">", "sma_100"), c("rsi_14", ">", 35)), EXIT(5, 7, 8)),
    "lib-vshock": ("Volume shock, up day", "Unusual buying: 3× volume, up 2%+.", "Volume", "1D",
                   ALL(c("vol_ratio", ">", 3), c("ret_1", ">", 2)), EXIT(6, 12, 20)),
    "lib-gap": ("Gap up and hold", "Opens 2%+ higher on heavy volume and holds.", "Volume", "1D",
                ALL(c("gap_pct", ">", 2), c("vol_ratio", ">", 2), sig("is_green")), EXIT(5, 10, 10)),
    "lib-dry": ("Volume dry-up in a leader", "Quiet, low-volume day near highs: sellers gone.", "Volume", "1D",
                ALL(c("vol_ratio", "<", 0.5), c("dist_hh52_pct", ">", -8), ff("close", ">", "sma_50")), EXIT(5, 12, 20)),
    "lib-deliv": ("Delivery-backed breakout", "New 20-day high where most shares were taken for delivery.", "Volume", "1D",
                  ALL(ff("close", ">", "hh_20"), c("delivery_pct", ">", 55), c("delivery_ratio", ">", 1.5)), EXIT(6, 12, 20)),
    "lib-insider": ("Insiders buying, trend up", "Insiders bought over ₹50 lakh in 30 days and price is above its 50-day.", "Events", "1D",
                    ALL(c("insider_buy_cr_30d", ">", 0.5), ff("close", ">", "sma_50")), EXIT(8, 20, 60)),
    "lib-promoter": ("Promoter adding shares", "Promoter stake up this quarter, no pledge, above the 200-day.", "Owners", "1D",
                     ALL(c("promoter_change_qoq", ">", 0.25), c("pledged_pct", "<", 1), ff("close", ">", "sma_200")), EXIT(8, 20, 60)),
    "lib-funds": ("Funds accumulating", "FIIs and DIIs both added this quarter; stock near highs.", "Owners", "1D",
                  ALL(c("fii_change_qoq", ">", 0.5), c("dii_change_qoq", ">", 0.5), c("dist_hh52_pct", ">", -10)), EXIT(8, 18, 60)),
    "lib-orders": ("Order win momentum", "An order win in the last 10 days and price breaking a 20-day high.", "Events", "1D",
                   ALL(c("days_since_order_win", "<=", 10), ff("close", ">", "hh_20")), EXIT(7, 15, 30)),
    "lib-quality": ("Quality in an uptrend", "ROCE above 20%, low debt, price above its 200-day.", "Business", "1D",
                    ALL(c("roce", ">", 20), c("debt_to_equity", "<", 0.5), ff("close", ">", "sma_200"), c("ret_120", ">", 0)), EXIT(10, 25, 90)),
    "lib-growth": ("Earnings growth breakout", "Profit up 25%+ YoY and a fresh 52-week high.", "Business", "1D",
                   ALL(c("profit_growth_yoy", ">", 25), ff("close", ">", "hh_52")), EXIT(8, 20, 60)),
    "lib-value": ("Cheap and turning", "P/E under 15, ROE above 15%, crossing above its 50-day.", "Business", "1D",
                  ALL(c("pe", "<", 15), c("pe", ">", 0), c("roe", ">", 15), ff("close", "crosses_above", "sma_50")), EXIT(8, 18, 60)),
    "lib-postres": ("Post-results breakout", "Within 5 days of results, closing at a 20-day high on volume.", "Events", "1D",
                    ALL(c("days_since_results", "<=", 5), ff("close", ">", "hh_20"), c("vol_ratio", ">", 1.5)), EXIT(6, 14, 25)),
}
META = {k: {"about": v[1], "family": v[2]} for k, v in LIBRARY.items()}


def ensure(cfg, con, key: str) -> str:
    name, about, family, tf, entry, exit_ = LIBRARY[key]
    entry, exit_ = normalise(entry), normalise_exit(exit_)
    row = con.execute("SELECT id, current_version FROM strategies WHERE kind='library' AND name=? AND archived=0", (name,)).fetchone()
    if not row:
        return service.create_strategy(con, cfg, None, name, tf, entry, exit_, kind="library", visibility="public")["id"]
    cur = con.execute("SELECT entry_json, exit_json FROM strategy_versions WHERE id=?", (row["current_version"],)).fetchone()
    if json.loads(cur["entry_json"]) != entry or json.loads(cur["exit_json"]) != exit_:
        service.new_version(con, row["id"], entry_json=jdump(entry), exit_json=jdump(exit_), rule_hash=rule_hash(entry, tf),
                            ai_enhancement_id=None, ai_gate=None)
    return row["id"]


def meta_for(name: str) -> dict:
    for k, v in LIBRARY.items():
        if v[0] == name:
            return {"key": k, **META[k]}
    return {}


def build(cfg, con, log=print) -> list[dict]:
    """Create/refresh every library strategy and compute its past test (cached per data date)."""
    out = []
    for key, (name, _about, _fam, tf, *_r) in LIBRARY.items():
        try:
            sid = ensure(cfg, con, key)
            v = con.execute("SELECT v.* FROM strategies s JOIN strategy_versions v ON v.id=s.current_version WHERE s.id=?", (sid,)).fetchone()
            bt = service.run_backtest(cfg, con, json.loads(v["entry_json"]), json.loads(v["exit_json"]), tf, v["id"])
            a = (bt.get("summary") or {}).get("All") or {}
            out.append({"key": key, "id": sid, "name": name, "trades": a.get("trades", 0), "win_rate": a.get("win_rate"), "ok": True})
        except Exception as e:  # noqa: BLE001 - one entry must never stop the rest
            out.append({"key": key, "name": name, "ok": False, "error": f"{type(e).__name__}: {e}"})
        log(json.dumps(out[-1], default=str))
    return out
