# Deployment Complete: L0 Production & L5 Shadow 90-Day Observation

## Deployment Summary
- **Deployment Date:** 2026-09-14
- **Observation Start Date:** 2026-09-15 (First trading day following deployment)
- **Primary Strategy:** L0 (Strict Rules Baseline) — Tested at +93.4R/year, PF 1.63, WR 36.4%, Max DD 21.4R
- **Shadow Strategy:** L5 (ML Engine v2) — Tested at +86.8R/year, PF 1.52, WR 54.3%, Max DD 26.7R
- **Observation Window:** 90 calendar/trading days with zero parameter modifications or model retuning.

---

## Frozen Artifact Hashes (SHA-256)

### L0 Production Config
| Artifact | SHA-256 Hash |
| :--- | :--- |
| `QuantSync-V6/config_live.json` | `00323d5f2e7ec257f15dd1f9536664864f7d244ad584f49101aff4c1d9b05483` |
| `deploy/l0_l5_observation/l0_config_FROZEN.json` | `00323d5f2e7ec257f15dd1f9536664864f7d244ad584f49101aff4c1d9b05483` |

### L5 ML Engine v2 Shadow Artifacts
| Artifact | SHA-256 Hash |
| :--- | :--- |
| `meta_model.lgb` | `a8d6c84825cf06e04ebba88d658967cda761320c411ffa5fbfcec6980a760d4d` |
| `pattern_HTF.lgb` | `80d62a2ce96bf76ef129ec9c47785d19f91f30770c925c0e34ab40f7c5d9038a` |
| `pattern_BASE.lgb` | `b8ba16f532e9e66e37d172ae2bbb47b66172a845cbfac44e39b8ecbcb9f524da` |
| `pattern_52W_HIGH.lgb` | `722e634c5b74b6290993c225c8eb16b763a536c64a19fb39b99c087af8c2eff7` |
| `pattern_DARVAS.lgb` | `777bf76a620c9d5df9cad584c59aaa756f244d765b00fe6e5c96c2cf7b4133d2` |
| `regressor_mfe_r.lgb` | `d919d2fe3275943d19b076daa152b9c117458ad69f8716d88b011c4d3dadecb9` |
| `regressor_mae_r.lgb` | `33ea7a83bbaa3b99b00107d0ffd8c2667fcfe0e152790c739f16be63d68dfb0a` |
| `regressor_days_to_peak.lgb` | `c0a4676469c2aef7ad99681ac1ed1511080012bf349ccbf4526f508a364c8487` |
| `historical_shapes.npy` | `1ceecf2c1ba66aa975d55bd45db6a4a0719e5b4d39e74a095993ee49fe3be45d` |
| `historical_outcomes.npy` | `1cc8a09ed317b16e476c814353d98b38a606c9524c08d3ee17d73013c04546b1` |
| `feature_list.json` | `98d482a876fa60241297d54b5ec79083bf660a14195a69f6fa9e52ae31a1221e` |
| `model_card.json` | `daeccc572c5075bb94ee7b85c7230afdba17a934cf764e0953bf0775f04153ad` |

---

## Verification Status

| Verification Test | Command | Status |
| :--- | :--- | :---: |
| **Observation Freeze Test** | `python3 deploy/l0_l5_observation/test_observation_freeze.py` | **PASSED (100%)** |
| **Engine Freeze Test** | `python3 QuantSync_Lines/banana_engine/tests/test_freeze.py` | **PASSED (100%)** |
| **L5 Shadow Pipeline** | `python3 deploy/l0_l5_observation/run_shadow_daily.py` | **PASSED** |
| **Daily Outcome Resolver** | `python3 deploy/l0_l5_observation/daily_resolver.py` | **PASSED** |
| **Observation Status API** | `curl -s http://localhost:8088/api/observation/status` | **HTTP 200 OK** |
| **Observation Web View** | `curl -s http://localhost:8088/observation` | **HTTP 200 OK** |

---

## Daily Schedule & Operations

### Scheduled Jobs (Cron)
```cron
# Market close runs at 4:30 PM IST (11:00 UTC) Monday-Friday
0 11 * * 1-5 cd /Users/rahultiwari/Projects/WIX_with_local/wix_latest_code/QuantSync && python3 deploy/l0_l5_observation/run_shadow_daily.py >> deploy/l0_l5_observation/logs/shadow.log 2>&1
5 11 * * 1-5 cd /Users/rahultiwari/Projects/WIX_with_local/wix_latest_code/QuantSync && python3 deploy/l0_l5_observation/daily_resolver.py >> deploy/l0_l5_observation/logs/resolver.log 2>&1
```

### Dashboard Access
- Live Observation Status: [http://localhost:8088/observation](http://localhost:8088/observation)
- API Observation Endpoint: [http://localhost:8088/api/observation/status](http://localhost:8088/api/observation/status)

---

## 90-Day Evaluation Rule
The 90-day observation window runs from **2026-09-15** through **2026-12-14**.
At day 90:
- Ship L5 as primary **ONLY IF**:
  1. L5 live annual R > L0 live annual R by $\ge 10\%$
  2. L5 live Profit Factor $\ge 1.3$
  3. L5 live max DD $\le 1.3 \times$ L0 live max DD
- Otherwise: Maintain L0 strict rules as primary.
