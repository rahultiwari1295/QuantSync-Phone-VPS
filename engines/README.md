# QuantSync Engine Developer Guide
==================================
**How to Build, Test Locally, and Deploy New Trading Engines to Prod_VPS**

---

## 1. Plugin Architecture & Auto-Discovery

The `Prod_VPS` platform is designed with a plug-and-play architecture. Any trading system can be added as an independent engine without modifying the scheduler, broker, Telegram alerts, or Wix integration.

### How Engine Discovery Works (`registry.py`)
1. The `EngineRegistry` (`engines/registry.py`) scans every subdirectory in `engines/`.
2. If a subfolder contains an `engine.py` file, it dynamically imports it.
3. It checks for a factory function named `get_engine(config)`.
4. If found, it instantiates the engine and registers it under the folder name (e.g. `v2`, `v3`, `v4`).
5. Engines can be enabled or disabled via `config.json`.

---

## 2. The Standard Engine Contract (`BaseTradingEngine`)

Every new engine **MUST** inherit from `BaseTradingEngine` defined in `engines/base_engine.py`:

```python
from engines.base_engine import BaseTradingEngine
```

### Required Methods & Exact Signatures

| Method Name | Signature | Purpose & Required Return Value |
| :--- | :--- | :--- |
| **`get_engine`** *(Top-Level)* | `get_engine(config: Optional[Dict] = None) -> BaseTradingEngine` | **Mandatory module-level factory function**. Instantiates and returns your engine. |
| **`__init__`** | `__init__(self, engine_id, engine_name, initial_capital, capital_per_position, max_slots, config)` | Initializes capital, max slots, positions, and storage. |
| **`step`** | `step(self, current_date: str, is_live: bool = False) -> Dict[str, Any]` | **Core execution method**. Advances the portfolio by 1 market day (evaluates exits, evaluates new entries, deducts friction). Must return standard dictionary (see below). |
| **`run`** | `run(self, start_date: str, end_date: str, is_live: bool = False, **kwargs) -> Dict[str, Any]` | Universal execution runner: if `start_date == end_date`, calls `step()`; if `start_date < end_date`, runs multi-day loop. |
| **`get_active_symbols`**| `get_active_symbols(self) -> List[str]` | Returns list of stock ticker symbols currently held in open positions (e.g. `['RELIANCE', 'TCS']`). |
| **`check_exits`** | `check_exits(self, ltp_map: Dict[str, float]) -> List[Dict[str, Any]]` | Evaluates real-time intraday LTP quotes against active stops and targets. Returns list of triggered exits. |
| **`get_trading_dates`**| `get_trading_dates(self, start_date: str, end_date: str) -> List[str]` | Returns sorted list of market dates (`YYYY-MM-DD`) between `start_date` and `end_date`. |
| **`reset`** | `reset(self, initial_capital: Optional[float] = None)` | Resets internal portfolio state (active positions, cash, closed trades) for clean replays. |
| **`export_reports`** | `export_reports(self, output_dir: str, prefix: str)` | Exports trade ledger CSV and summary JSON to `reports/`. |
| **`print_summary`** | `print_summary(self)` | Prints formatted console summary of win rate, net profit, and profit factor. |

---

## 3. Required Output Format for `step()`

When `step(current_date, is_live)` is called, it **MUST return a dictionary** containing these exact keys:

```python
return {
    "date": current_date,                # "YYYY-MM-DD"
    "nav": float(self.current_nav),      # Total current NAV (Cash + Invested Capital)
    "cash": float(self.cash),            # Uninvested liquid cash
    "invested_capital": float(inv_cap),  # Total invested capital at cost/market
    "exits_count": len(exits_today),     # Number of positions closed today
    "entries_count": len(entries_today), # Number of new positions opened today
    "open_positions_count": len(open_p), # Total open positions active right now
    "exits": exits_today,                # List of closed trade dicts (see schema below)
    "entries": entries_today             # List of new active trade dicts (see schema below)
}
```

### Trade Record Schema

#### New Entry Dict (`entries_today`):
```python
{
    "trade_id": "T_00001",
    "symbol": "TATASTEEL",
    "strategy_id": "Breakout",
    "entry_date": "2026-09-04",
    "entry_price": 155.50,
    "stop_price": 145.00,
    "target_price": 180.00,
    "qty": 64,
    "allocated_capital": 9952.00,
    "cost_basis": 9959.46,      # Includes statutory entry friction
    "entry_cost": 7.46,         # 7.5 bps entry friction
    "days_held": 0
}
```

#### Closed Exit Dict (`exits_today`):
```python
{
    "trade_id": "T_00001",
    "symbol": "TATASTEEL",
    "strategy_id": "Breakout",
    "entry_date": "2026-08-20",
    "entry_price": 155.50,
    "exit_date": "2026-09-04",
    "exit_price": 172.20,
    "exit_reason": "TARGET_HIT",
    "qty": 64,
    "gross_pnl": 1068.80,
    "total_costs": 15.73,       # 15 bps round-trip friction
    "realized_pnl": 1053.07,    # Net realized profit after friction
    "return_pct": 10.74,
    "days_held": 11
}
```

---

## 4. Step-by-Step: How to Create, Test & Deploy an Engine

### Step 1: Create the Engine Directory
Create a new folder in `Prod_VPS/engines/` (e.g. `v4`):
```bash
mkdir -p Prod_VPS/engines/v4
```

### Step 2: Implement `engine.py`
Create `Prod_VPS/engines/v4/engine.py` following the boilerplate template below.

### Step 3: Register in `config.json`
Add your engine configuration block to `Prod_VPS/config.json`:
```json
"engines": {
  "v4": {
    "enabled": true,
    "name": "QuantSync-V4",
    "capital": 300000.0,
    "capital_per_pos": 10000.0,
    "max_slots": 30,
    "telegram": {
      "enabled": true,
      "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
      "chat_id": "@your_channel"
    }
  }
}
```

### Step 4: Test Locally
Run these commands locally to verify full compatibility:

```bash
# 1. Test that the plugin is auto-discovered without errors:
python3 -c "from engines.registry import EngineRegistry; reg = EngineRegistry(); print(reg.discover_and_load().keys())"

# 2. Test a single-day dry-run (paper execution):
python3 cli.py --engine v4 --date 2026-09-04 --dry-run

# 3. Test a historical replay simulation:
python3 cli.py --replay --engine v4 --start 2026-01-01 --end 2026-09-04
```

### Step 5: Direct Copy-Paste to Production VPS
Once verified locally, directly sync the folder to the live server:
```bash
# Copy engine directory to VPS:
scp -r -P 20006 Prod_VPS/engines/v4 root@162.19.81.219:/root/quantsync/engines/

# Update config.json on VPS:
scp -P 20006 Prod_VPS/config.json root@162.19.81.219:/root/quantsync/config.json

# Restart daemon on VPS to make it live:
ssh -p 20006 root@162.19.81.219 "systemctl restart quantsync && systemctl status quantsync"
```

---

## 5. Complete Copy-Paste Boilerplate Template

You can copy this complete, functional boilerplate directly to start building a new engine:

```python
"""
engines/v4/engine.py — Production Engine Boilerplate
===================================================
"""

import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional

_ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINES_DIR = os.path.dirname(_ENGINE_DIR)
_PROD_ROOT = os.path.dirname(_ENGINES_DIR)
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from engines.base_engine import BaseTradingEngine
from common.storage import MasterStorage
from common.notifier import notify_buy, notify_exit

COST_RATE_PER_SIDE = 0.00075  # 7.5 bps per side = 15 bps round-trip


class MyTradingEngine(BaseTradingEngine):
    """
    Template implementation of BaseTradingEngine.
    """
    def __init__(
        self,
        engine_id: str = "v4",
        engine_name: str = "QuantSync-V4",
        initial_capital: float = 300000.0,
        capital_per_position: float = 10000.0,
        max_slots: int = 30,
        config: Optional[Dict[str, Any]] = None
    ):
        super().__init__(engine_id, engine_name, initial_capital, capital_per_position, max_slots, config)
        self.storage = MasterStorage()
        self.db_path = os.path.join(_PROD_ROOT, "Data", "stock_history.db")
        self.reset()

    def reset(self, initial_capital: Optional[float] = None):
        self.initial_capital = initial_capital or self.initial_capital
        self.cash = self.initial_capital
        self.peak_equity = self.initial_capital
        self.active_positions: List[Dict[str, Any]] = []
        self.closed_trades: List[Dict[str, Any]] = []
        self.trade_counter = 0

    def get_trading_dates(self, start_date: str, end_date: str, include_settlement: bool = True) -> List[str]:
        # Connect to SQLite to retrieve trading dates from Nifty
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT rawCandles FROM StockHistory WHERE symbol = 'NIFTY' LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return [start_date]
        candles = json.loads(row[0])
        dates = sorted(list(set(c["t"][:10] for c in candles if start_date <= c["t"][:10] <= end_date)))
        return dates

    def get_active_symbols(self) -> List[str]:
        return [p["symbol"] for p in self.active_positions]

    def check_exits(self, ltp_map: Dict[str, float]) -> List[Dict[str, Any]]:
        """Intraday stop-loss / target monitor."""
        exits_triggered = []
        for pos in self.active_positions:
            sym = pos["symbol"]
            ltp = ltp_map.get(sym)
            if not ltp or ltp <= 0:
                continue
            if ltp <= pos["stop_price"]:
                exits_triggered.append({"symbol": sym, "reason": "STOP_HIT", "price": ltp})
            elif pos.get("target_price", 0) > 0 and ltp >= pos["target_price"]:
                exits_triggered.append({"symbol": sym, "reason": "TARGET_HIT", "price": ltp})
        return exits_triggered

    def step(self, current_date: str, is_live: bool = False) -> Dict[str, Any]:
        """Executes a single market session on current_date."""
        exits_today = []
        entries_today = []

        # 1. EVALUATE EXITS FOR ACTIVE POSITIONS
        still_active = []
        for p in self.active_positions:
            # Skip positions entered today
            if p["entry_date"] == current_date:
                still_active.append(p)
                continue

            p["days_held"] += 1
            # In live execution, fetch today's candle or use current bar
            # (Insert your exit evaluation logic here)
            is_exit = False
            exit_price = p["entry_price"]
            exit_reason = "EXIT"

            if is_exit:
                gross = exit_price * p["qty"]
                exit_cost = gross * COST_RATE_PER_SIDE
                net_proceeds = gross - exit_cost
                self.cash += net_proceeds

                realized_pnl = net_proceeds - p["cost_basis"]
                p["exit_date"] = current_date
                p["exit_price"] = round(exit_price, 2)
                p["exit_reason"] = exit_reason
                p["realized_pnl"] = round(realized_pnl, 2)
                p["return_pct"] = round((exit_price / p["entry_price"] - 1.0) * 100.0, 2)
                p["engine_version"] = self.engine_id

                self.closed_trades.append(p)
                exits_today.append(p)

                if is_live:
                    self.storage.close_trade(p)
                    notify_exit(p, engine_version=self.engine_id)
            else:
                still_active.append(p)

        self.active_positions = still_active

        # 2. SCAN & ENTER CANDIDATES
        avail_slots = self.max_slots - len(self.active_positions)
        if avail_slots > 0:
            # (Insert your setup candidate scanner or ML scoring logic here)
            candidate_list = []  # e.g., list of setup dicts

            for cand in candidate_list:
                if avail_slots <= 0:
                    break
                sym = cand["symbol"]
                if sym in self.get_active_symbols():
                    continue

                ep = cand["entry_price"]
                qty = int(self.capital_per_position / ep)
                if qty <= 0:
                    continue

                cost_basis = qty * ep
                entry_cost = cost_basis * COST_RATE_PER_SIDE
                total_req = cost_basis + entry_cost

                if self.cash < total_req:
                    continue

                self.cash -= total_req
                self.trade_counter += 1

                new_pos = {
                    "trade_id": f"T_{self.trade_counter:05d}",
                    "engine_version": self.engine_id,
                    "symbol": sym,
                    "strategy_id": cand.get("strategy_id", "Custom"),
                    "entry_date": current_date,
                    "entry_price": round(ep, 2),
                    "stop_price": round(cand["stop_price"], 2),
                    "target_price": round(cand.get("target_price", 0.0), 2),
                    "qty": qty,
                    "shares": qty,
                    "allocated_capital": round(cost_basis, 2),
                    "invested_capital": round(cost_basis, 2),
                    "cost_basis": round(total_req, 2),
                    "entry_cost": round(entry_cost, 2),
                    "days_held": 0
                }
                self.active_positions.append(new_pos)
                avail_slots -= 1
                entries_today.append(new_pos)

                if is_live:
                    self.storage.create_active_trade(new_pos)
                    notify_buy(new_pos, engine_version=self.engine_id)

        # 3. COMPUTE MARK-TO-MARKET EOD NAV
        invested = sum(p["qty"] * p["entry_price"] for p in self.active_positions)
        curr_nav = self.cash + invested
        self.peak_equity = max(self.peak_equity, curr_nav)

        if is_live:
            dd_inr = self.peak_equity - curr_nav
            dd_pct = (dd_inr / self.peak_equity * 100.0) if self.peak_equity > 0 else 0.0
            self.storage.record_daily_nav({
                "date": current_date,
                "engine_version": self.engine_id,
                "cash": round(self.cash, 2),
                "invested_capital": round(invested, 2),
                "total_nav": round(curr_nav, 2),
                "active_positions_count": len(self.active_positions),
                "drawdown_inr": round(dd_inr, 2),
                "drawdown_pct": round(dd_pct, 2),
                "peak_equity": round(self.peak_equity, 2)
            })

        return {
            "date": current_date,
            "nav": round(curr_nav, 2),
            "cash": round(self.cash, 2),
            "invested_capital": round(invested, 2),
            "exits_count": len(exits_today),
            "entries_count": len(entries_today),
            "open_positions_count": len(self.active_positions),
            "exits": exits_today,
            "entries": entries_today
        }

    def run(self, start_date: str, end_date: str, is_live: bool = False, **kwargs) -> Dict[str, Any]:
        if start_date == end_date:
            return self.step(start_date, is_live=is_live)

        self.reset()
        dates = self.get_trading_dates(start_date, end_date)
        for dt in dates:
            self.step(dt, is_live=is_live)

        net_profit = (self.cash + sum(p["qty"] * p["entry_price"] for p in self.active_positions)) - self.initial_capital
        return {
            "engine_id": self.engine_id,
            "engine_name": self.engine_name,
            "starting_capital": self.initial_capital,
            "ending_nav": round(self.initial_capital + net_profit, 2),
            "net_profit_inr": round(net_profit, 2),
            "completed_trades": len(self.closed_trades),
            "trades": self.closed_trades
        }

    def export_reports(self, output_dir: str, prefix: str):
        os.makedirs(output_dir, exist_ok=True)
        if self.closed_trades:
            df = pd.DataFrame(self.closed_trades)
            df.to_csv(os.path.join(output_dir, f"{prefix}_trades.csv"), index=False)

    def print_summary(self):
        print(f"[{self.engine_name}] Total Trades: {len(self.closed_trades)} | Cash: ₹{self.cash:,.2f}")


def get_engine(config: Optional[Dict[str, Any]] = None) -> BaseTradingEngine:
    """Factory entrypoint required by EngineRegistry."""
    cfg = config or {}
    return MyTradingEngine(
        engine_id=cfg.get("engine_id", "v4"),
        engine_name=cfg.get("name", "QuantSync-V4"),
        initial_capital=float(cfg.get("capital", 300000.0)),
        capital_per_position=float(cfg.get("capital_per_pos", 10000.0)),
        max_slots=int(cfg.get("max_slots", 30)),
        config=cfg
    )
```
