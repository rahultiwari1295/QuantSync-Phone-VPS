"""State machine tests with synthetic bars. Offsets are in ATRs so the tests don't depend on a magic price."""
import numpy as np
import pytest

from common import trade_rules as tr

LEVEL = 110.0


def history(trend=True):
    """300 bars of uptrend 70->108, then 40 bars of base below LEVEL (so 52w high = ~LEVEL-ish, trend is up)."""
    up = np.linspace(70, 106, 300) if trend else np.linspace(150, 106, 300)
    base = 106 + 1.5 * np.sin(np.arange(40) * 1.1)
    return list(np.r_[up, base])


def build(closes):
    c = np.array(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.6
    l = np.minimum(o, c) - 0.6
    v = np.full(len(c), 1000.0)
    return o, h, l, c, v


def run(extra, trend=True, level=LEVEL, edge={}, **kw):
    o, h, l, c, v = build(history(trend) + list(extra))
    return tr.evaluate_arrays(o, h, l, c, v, level, "D", edge=edge, **kw)


def atr_of(extra=()):
    o, h, l, c, v = build(history() + list(extra))
    return float(tr.atr_series(h, l, c)[-1])


def test_forming_far_below():
    assert run([])["state"] in ("FORMING", "BUILDING", "NEAR")


def test_near():
    a = atr_of()
    r = run([LEVEL - 0.8 * a])
    assert r["state"] == "NEAR" and r["action"] == "WATCH"


def test_attempt_is_not_a_breakout():
    o, h, l, c, v = build(history() + [LEVEL - 0.3])
    h[-1] = LEVEL + 1.0                                    # poked above, closed below
    r = tr.evaluate_arrays(o, h, l, c, v, LEVEL, "D", edge={})
    assert r["state"] == "ATTEMPT" and r["action"] == "WATCH"


def test_decisive_breakout_is_buy_grade_a():
    a = atr_of()
    table = {"cells": {"D|BREAKOUT|A": {"verdict": "VALIDATED", "vs_baseline_R": 0.18, "n": 1419, "win_rate": .47,
                                         "hit_target": .28, "hit_ci95": [.26, .30], "mean_R": .195, "avg_risk_pct": 10.0}}}
    r = run([LEVEL - 1, LEVEL + 1.5 * a], edge=table)
    assert r["state"] == "BREAKOUT" and r["action"] == "BUY" and r["grade"] == "A"
    p = r["plan"]
    assert p["stop"] < p["entry_ref"] < p["target"]
    assert abs((p["target"] - p["entry_ref"]) / (p["entry_ref"] - p["stop"]) - tr.TARGET_R) < 0.01


def test_marginal_close_is_weak_not_buy():
    a = atr_of()
    r = run([LEVEL - 1, LEVEL + 0.2 * a])
    assert r["state"] == "WEAK_BREAKOUT" and r["action"] == "AVOID" and r["grade"] == "C"


def test_gate_off_keeps_state_for_measurement():
    a = atr_of()
    r = run([LEVEL - 1, LEVEL + 0.2 * a], grade_gate=False)
    assert r["state"] == "BREAKOUT" and r["grade"] == "C"


def test_below_trend_is_grade_b():
    a = atr_of()
    r = run([LEVEL - 1, LEVEL + 1.5 * a], trend=False)
    assert r["grade"] == "B"


def test_failed_breakout():
    a = atr_of()
    r = run([LEVEL - 1, LEVEL + 1.5 * a, LEVEL + 0.5 * a, LEVEL - 0.6 * a])
    assert r["state"] == "FAILED" and r["action"] == "AVOID"


def test_retest_hold_is_recognised_but_only_a_buy_if_validated():
    a = atr_of()
    o, h, l, c, v = build(history() + [LEVEL - 1, LEVEL + 1.5 * a, LEVEL + 2.2 * a, LEVEL + 0.9 * a, LEVEL + 0.4 * a])
    # last bar: dips to the level, closes back above on a green candle
    o[-1] = LEVEL + 0.1 * a
    l[-1] = LEVEL + 0.2 * a - 0.0
    c[-1] = LEVEL + 0.45 * a
    h[-1] = c[-1] + 0.1
    r = tr.evaluate_arrays(o, h, l, c, v, LEVEL, "D", edge={})
    # measured: daily retests have NO validated edge over the plain breakout -> state is recognised, but it is not a BUY
    assert r["state"] == "RETEST_HOLD" and r["kind"] == "RETEST" and r["action"] == "WATCH"
    assert r["plan"]["stop"] < LEVEL
    table = {"cells": {"D|RETEST|A": {"verdict": "VALIDATED", "vs_baseline_R": 0.2, "n": 400, "win_rate": .5,
                                       "hit_target": .3, "mean_R": .2, "avg_risk_pct": 8}}}
    r2 = tr.evaluate_arrays(o, h, l, c, v, LEVEL, "D", edge=table)
    assert r2["action"] == "BUY"


def test_retest_watch_when_back_at_level_on_red_candle():
    a = atr_of()
    o, h, l, c, v = build(history() + [LEVEL - 1, LEVEL + 1.5 * a, LEVEL + 2.0 * a, LEVEL + 0.8 * a, LEVEL + 0.3 * a])
    o[-1] = LEVEL + 0.9 * a                                # red candle that touches the zone
    c[-1] = LEVEL + 0.3 * a
    l[-1] = LEVEL + 0.2 * a
    h[-1] = o[-1] + 0.1
    r = tr.evaluate_arrays(o, h, l, c, v, LEVEL, "D", edge={})
    assert r["state"] == "RETEST_WATCH" and r["action"] == "WATCH"


def test_stop_and_target_resolution():
    a = atr_of()
    up = [LEVEL - 1, LEVEL + 1.5 * a, LEVEL + 3 * a, LEVEL + 12 * a]
    assert run(up)["state"] == "TARGET_ACHIEVED"
    down = [LEVEL - 1, LEVEL + 1.5 * a, LEVEL - 3 * a]
    assert run(down)["state"] in ("STOPPED", "FAILED")


def test_old_breakout_is_not_a_fresh_entry():
    a = atr_of()
    r = run([LEVEL - 1] + [LEVEL + (1.5 + 0.1 * k) * a for k in range(80)])
    assert r["state"] == "EXTENDED" and r["action"] == "AVOID"


def test_running_after_entry_window_has_no_new_buy():
    a = atr_of()
    r = run([LEVEL - 1, LEVEL + 1.5 * a] + [LEVEL + 2 * a] * 6)
    assert r["state"] == "RUNNING" and r["action"] == "HOLD"


def test_bearish_or_missing_level_is_inert():
    o, h, l, c, v = build(history())
    assert tr.evaluate_arrays(o, h, l, c, v, 0, "D")["state"] == "NA"
    assert tr.evaluate_arrays(o, h, l, c, v, LEVEL, "D", stance="BEARISH")["state"] == "NA"


def test_no_lookahead_future_bars_do_not_change_past_result():
    a = atr_of()
    ext = [LEVEL - 1, LEVEL + 1.5 * a, LEVEL + 2.5 * a, LEVEL - 4 * a]
    o, h, l, c, v = build(history() + ext)
    n = len(c) - 2                                          # evaluate as of the breakout bar + 1
    now = tr.evaluate_arrays(o[:n], h[:n], l[:n], c[:n], v[:n], LEVEL, "D", edge={})
    o2, h2, l2, c2, v2 = build(history() + ext[:2])
    same = tr.evaluate_arrays(o2, h2, l2, c2, v2, LEVEL, "D", edge={})
    assert now["state"] == same["state"] and now["grade"] == same["grade"]


def test_grade_function():
    assert tr.grade_event(0.5, 0, True) == "C"
    assert tr.grade_event(1.0, 1.0, True) == "A"
    assert tr.grade_event(1.0, 5.0, True) == "B"
    assert tr.grade_event(1.0, 1.0, False) == "B"


def test_wilson_interval_sane():
    lo, hi = tr.wilson(30, 100)
    assert 0.2 < lo < 0.3 < hi < 0.4
    assert tr.wilson(0, 0) == [0.0, 0.0]


def test_resolve_timeframes_daily_buy_blocked_by_failed_weekly():
    calls = {"D": {"state": "BREAKOUT", "action": "BUY", "grade": "A"},
             "W": {"state": "FAILED", "action": "AVOID", "grade": "A"}}
    r = tr.resolve_timeframes(calls)
    assert r["primary_tf"] == "D" and r["action"] == "WATCH" and r["conflict"]


def test_resolve_timeframes_single_decision():
    calls = {"D": {"state": "NEAR", "action": "WATCH"}, "M": {"state": "BUILDING", "action": "WATCH"}}
    r = tr.resolve_timeframes(calls)
    assert r["primary_tf"] == "D" and r["action"] == "WATCH"


def test_buy_is_withheld_when_edge_table_says_no_edge():
    a = atr_of()
    table = {"cells": {"D|BREAKOUT|A": {"verdict": "NO_EDGE", "vs_baseline_R": -0.1, "n": 500, "win_rate": .3,
                                         "hit_target": .2, "mean_R": -.05, "avg_risk_pct": 8}}}
    r = run([LEVEL - 1, LEVEL + 1.5 * a], edge=table)
    assert r["state"] == "BREAKOUT" and r["action"] == "WATCH" and r["confidence"]["label"] == "No validated edge"


def test_buy_allowed_when_validated_and_beats_baseline():
    a = atr_of()
    table = {"cells": {"D|BREAKOUT|A": {"verdict": "VALIDATED", "vs_baseline_R": 0.18, "n": 1419, "win_rate": .47,
                                         "hit_target": .28, "hit_ci95": [.26, .30], "mean_R": .195, "avg_risk_pct": 10.0}}}
    r = run([LEVEL - 1, LEVEL + 1.5 * a], edge=table)
    assert r["action"] == "BUY" and r["confidence"]["label"] == "Validated edge" and "1,419" in r["confidence"]["text"]


def test_validated_but_not_better_than_random_is_not_a_buy():
    a = atr_of()
    table = {"cells": {"D|BREAKOUT|A": {"verdict": "VALIDATED", "vs_baseline_R": 0.02, "n": 900, "win_rate": .4,
                                         "hit_target": .3, "mean_R": .1, "avg_risk_pct": 8}}}
    assert run([LEVEL - 1, LEVEL + 1.5 * a], edge=table)["action"] == "WATCH"
