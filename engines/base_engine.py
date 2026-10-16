"""
engines/base_engine.py — Standard Engine Interface / Contract
=============================================================
Every engine in Prod_VPS (V2, V3, V4, etc.) must implement this interface.
This ensures 100% code parity between:
  - Multi-Year Historical Replay (start_date < end_date)
  - Daily Live Production Session (start_date == end_date)
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Any, Optional


class BaseTradingEngine(ABC):
    """
    Abstract Base Class for all QuantSync trading engines.
    """
    def __init__(
        self,
        engine_id: str,
        engine_name: str,
        initial_capital: float = 300000.0,
        capital_per_position: float = 10000.0,
        max_slots: int = 30,
        config: Optional[Dict[str, Any]] = None
    ):
        self.engine_id = engine_id
        self.engine_name = engine_name
        self.initial_capital = float(initial_capital)
        self.capital_per_position = float(capital_per_position)
        self.max_slots = int(max_slots)
        self.config = config or {}

    @abstractmethod
    def run(
        self,
        start_date: str,
        end_date: str,
        is_live: bool = False,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Universal execution entrypoint:
          - If start_date == end_date: Executes single-day market session (today).
          - If start_date < end_date: Executes multi-day chronological replay.
        """
        pass

    @abstractmethod
    def step(self, current_date: str, is_live: bool = False) -> Dict[str, Any]:
        """Executes a single market session on current_date."""
        pass

    @abstractmethod
    def get_trading_dates(self, start_date: str, end_date: str, include_settlement: bool = True) -> List[str]:
        """Returns sorted list of market dates between start_date and end_date."""
        pass

    @abstractmethod
    def get_active_symbols(self) -> List[str]:
        """Returns list of stock symbols currently held in open positions."""
        pass

    @abstractmethod
    def check_exits(self, ltp_map: Dict[str, float]) -> List[Dict[str, Any]]:
        """Evaluates dynamic stop loss and profit targets against real-time LTP quotes."""
        pass

    @abstractmethod
    def export_reports(self, output_dir: str, prefix: str):
        """Exports ledger CSV and summary JSON."""
        pass

    @abstractmethod
    def print_summary(self):
        """Prints high-level execution KPI summary to console."""
        pass

    def get_candidates(self, date_str: str) -> List[Dict[str, Any]]:
        """Returns candidate buy signals generated on date_str for next-session execution."""
        return []
