# Model Limitations

## Survivorship bias

The universe contains only currently-listed symbols (1,318 valid symbols in `stock_history.db`). Delisted symbols from 2015-2025 are absent. This biases all backtest metrics optimistically by an unknown amount (typically 3-15% relative for equity strategies).

**Impact:** Walk-forward precision should be treated as an upper bound, not a forecast. Live results will be worse.

**Mitigation plan:** [Deferred — data acquisition out of scope for current pass.]

## Split adjustment

Historical prices in `stock_history.db` are pre-adjusted to 2026 corporate actions. This leaks future splits into historical price series. Pattern detection is largely unaffected (shapes survive adjustment), but absolute rupee amounts in historical simulations are unreliable.

**Impact:** All PnL and position sizes must be computed in R-multiples and percentages, never in absolute rupees.

**Mitigation plan:** [Deferred — raw data acquisition out of scope for current pass.]
