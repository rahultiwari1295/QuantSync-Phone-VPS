import numpy as np
import pandas as pd
from tests.engine_ref.p1_rs_leader import detect_rs_leader
from tests.engine_ref.p2_apex_base import detect_apex_base
from tests.engine_ref.p4_power_earnings_gap import detect_power_earnings_gap
from tests.engine_ref.p5_high_tight_flag import detect_high_tight_flag
from tests.engine_ref.p9_stage2_base_breakout import detect_stage2_base_breakout
from lab import features

def series(seed, n=1500):
    r=np.random.default_rng(seed)
    # regimes: long quiet bases then explosive bars, so breakouts / gaps / flags occur
    ret=r.standard_t(3,n)*0.012+0.0008
    for k in r.integers(60,n-5,12): ret[k]+=r.choice([0.04,0.07,0.12])
    for k in r.integers(60,n-40,3): ret[k:k+25]+=0.03          # strong run -> flagpole
    c=100*np.exp(np.cumsum(ret)); o=np.r_[c[0],c[:-1]]*(1+r.normal(0,0.004,n))
    gapk=r.integers(60,n,10); o[gapk]=c[gapk-1]*(1+r.uniform(0.035,0.09,10)); c[gapk]=np.maximum(c[gapk],o[gapk]*1.01)
    hi=np.maximum(o,c)*(1+np.abs(r.normal(0,0.002,n))); lo=np.minimum(o,c)*(1-np.abs(r.normal(0,0.002,n)))
    v=r.lognormal(13,0.5,n); big=np.r_[r.integers(60,n,25),gapk]; v[big]*=r.uniform(3.2,6,len(big))
    d=pd.bdate_range('2019-01-01',periods=n)
    return pd.DataFrame({'date':d,'open':o,'high':hi,'low':lo,'close':c,'volume':v})


MAP = {"p1": "sig_rs_leader", "p2": "sig_apex_base", "p4": "sig_power_gap", "p5": "sig_high_tight_flag", "p9": "sig_stage2_base"}


def test_signals_match_live_engine_bar_for_bar():
    fired = {k: 0 for k in MAP}
    for seed in range(14):
        df = series(seed)
        f = features.compute(df, "1D")
        c, o, h, l, v = [df[k].values for k in ("close", "open", "high", "low", "volume")]
        s20 = pd.Series(v).rolling(20, min_periods=1).mean().values
        s150 = pd.Series(c).rolling(150, min_periods=150).mean().values   # NaN until 150 bars, as in the live engine
        pcl = np.r_[c[0], c[:-1]]
        atr = pd.Series(np.maximum(h - l, np.maximum(abs(h - pcl), abs(l - pcl)))).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().values
        for i in range(len(df)):
            eng = {"p1": detect_rs_leader(df, i) is not None, "p2": detect_apex_base(df, i) is not None,
                   "p4": detect_power_earnings_gap(c, o, h, l, v, s20, atr, i) is not None,
                   "p5": detect_high_tight_flag(c, o, h, l, v, s20, atr, i) is not None,
                   "p9": detect_stage2_base_breakout(c, o, h, l, v, s20, s150, atr, i) is not None}
            for k, col in MAP.items():
                assert eng[k] == bool(f[col].iloc[i] == 1.0), (k, seed, i)
                fired[k] += eng[k]
    assert all(n >= 5 for n in fired.values()), fired   # the corpus must actually exercise every signal


def test_signals_are_point_in_time():
    df = series(3)
    full = features.compute(df, "1D")
    cut = features.compute(df.iloc[:900].copy(), "1D")
    for col in MAP.values():
        a, b = full[col].iloc[:900].fillna(-1).to_numpy(), cut[col].fillna(-1).to_numpy()
        assert (a == b).all(), col
