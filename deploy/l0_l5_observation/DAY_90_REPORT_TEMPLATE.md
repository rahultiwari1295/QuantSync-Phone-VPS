# 90-Day Observation Report

**Period:** <START> to <END> (<N> trading days)
**Systems:** L0 (rules, production) vs L5 (ML v2, shadow)

## Performance

| Metric | L0 | L5 | Delta |
|---|---|---|---|
| Total predictions | | | |
| Total resolved | | | |
| Win rate | | | |
| Mean realized R | | | |
| Median realized R | | | |
| Std dev of R | | | |
| Cumulative R | | | |
| Annualized R | | | |
| Profit factor | | | |
| Max drawdown (R) | | | |
| Sharpe of R stream | | | |

## Backtest vs Live

| Metric | L0 Backtest | L0 Live | Gap | L5 Backtest | L5 Live | Gap |
|---|---|---|---|---|---|---|
| Annual R | +93.4 | | | +86.8 | | |
| PF | 1.63 | | | 1.52 | | |
| WR | 36.4% | | | 54.3% | | |
| Max DD | 21.4R | | | 26.7R | | |

## Decision

Ship L5 as primary if:
- [ ] L5 live annual R > L0 live annual R by ≥ 10%
- [ ] L5 live PF ≥ 1.3
- [ ] L5 live max DD ≤ 1.3 × L0 live max DD

Otherwise: keep L0 as primary, continue observation.

## Notes
- [Anomalies observed]
- [Regime during observation]
- [Open questions]
