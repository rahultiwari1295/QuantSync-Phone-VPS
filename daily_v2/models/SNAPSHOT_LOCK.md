# DAILY V2 GATE 2B — PRODUCTION SNAPSHOT LOCK

**Snapshot ID:** `DAILY_V2_GATE_2B_FROZEN_2026_08_30`  
**Created:** August 30, 2026  
**Status:** **IMMUTABLE & APPROVED FOR VPS DEPLOYMENT / PAPER TRADING**

### Frozen Invariants:
- Model weights: `models/frozen_daily_v2_model.json` (SHA256: `b0ef60c3d7300fd6e4d2d30ad7060f8d09db02c6ebf6c1da9a1e3dba17b812c2`)
- Feature schema: `models/feature_names.json` (47 features, SHA256: `270b28821cd2f7c9ac839fab7471370aeea790888e8a2a378bb7656bc6655cc7`)
- Entry Rule: $P \ge 0.60$ + `plus_di14 > minus_di14` + `vol_ratio_20d >= 1.2` + Daily Top 3 Max
- Sizing Rule: Fixed ₹10,000 per trade
- Exit Engine: Frozen Daily V2 Health Engine (-8% stop, -5% early cut, EMA50 invalidation, MFE trailing)
