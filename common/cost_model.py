"""
common/cost_model.py — Transparent Statutory Indian Equity Transaction Cost Model
================================================================================
Calculates precise statutory Indian equity delivery friction:
  - Brokerage: 0 bps (discount) or 1.5 bps (flat)
  - STT: 0.1% (10 bps) on Buy and Sell delivery
  - Exchange Txn Fee (NSE): 0.00345%
  - GST: 18% on (Brokerage + Exchange Txn Fee)
  - SEBI Turnover Fee: Rs 10 per Crore (0.0001%)
  - Stamp Duty: 0.015% (1.5 bps) on Buy side only
  - Bid-Ask Spread: Half spread per leg
  - Slippage & Market Impact
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional


class FrictionScenario(str, Enum):
    LOW = "LOW"
    BASE = "BASE"
    HIGH = "HIGH"


@dataclass
class FrictionBreakdown:
    brokerage: float
    stt_tax: float
    exchange_txn_fee: float
    gst: float
    sebi_charges: float
    stamp_duty: float
    bid_ask_spread_cost: float
    slippage_cost: float
    market_impact_cost: float
    total_cost_cash: float
    total_cost_bps: float


class TransactionCostModel:
    """
    Authoritative Indian equity delivery statutory transaction cost model.
    """
    STT_RATE_DELIVERY = 0.0010         # 0.10% (10 bps) on buy and sell
    EXCHANGE_FEE_RATE = 0.0000345      # 0.00345% (0.345 bps)
    GST_RATE = 0.1800                  # 18% on (brokerage + exchange fee)
    SEBI_RATE = 0.0000010              # 0.0001% (0.01 bps)
    STAMP_DUTY_BUY_RATE = 0.000150     # 0.015% (1.5 bps) on buy side only

    SCENARIO_CONFIGS = {
        FrictionScenario.LOW: {
            "brokerage_bps": 0.0,
            "spread_bps": 2.0,
            "slippage_bps": 3.0,
            "market_impact_bps": 0.0,
        },
        FrictionScenario.BASE: {
            "brokerage_bps": 1.5,
            "spread_bps": 5.0,
            "slippage_bps": 5.0,
            "market_impact_bps": 1.0,
        },
        FrictionScenario.HIGH: {
            "brokerage_bps": 2.5,
            "spread_bps": 10.0,
            "slippage_bps": 12.0,
            "market_impact_bps": 3.0,
        },
    }

    @classmethod
    def calculate_leg_friction(
        cls,
        price: float,
        quantity: int,
        is_buy: bool,
        scenario: FrictionScenario = FrictionScenario.BASE,
        custom_slippage_bps: Optional[float] = None,
        custom_spread_bps: Optional[float] = None,
    ) -> FrictionBreakdown:
        if price <= 0.0 or quantity <= 0:
            return FrictionBreakdown(
                brokerage=0.0, stt_tax=0.0, exchange_txn_fee=0.0, gst=0.0,
                sebi_charges=0.0, stamp_duty=0.0, bid_ask_spread_cost=0.0,
                slippage_cost=0.0, market_impact_cost=0.0, total_cost_cash=0.0,
                total_cost_bps=0.0
            )

        notional = float(price * quantity)
        cfg = cls.SCENARIO_CONFIGS.get(scenario, cls.SCENARIO_CONFIGS[FrictionScenario.BASE])

        brokerage_bps = cfg["brokerage_bps"]
        brokerage = notional * (brokerage_bps / 10000.0)
        stt = notional * cls.STT_RATE_DELIVERY
        exch_fee = notional * cls.EXCHANGE_FEE_RATE
        gst = (brokerage + exch_fee) * cls.GST_RATE
        sebi = notional * cls.SEBI_RATE
        stamp = notional * cls.STAMP_DUTY_BUY_RATE if is_buy else 0.0

        spread_bps = custom_spread_bps if custom_spread_bps is not None else cfg["spread_bps"]
        slippage_bps = custom_slippage_bps if custom_slippage_bps is not None else cfg["slippage_bps"]
        impact_bps = cfg["market_impact_bps"]

        # Half spread per leg
        spread_cost = notional * (spread_bps / 20000.0)
        slippage_cost = notional * (slippage_bps / 10000.0)
        impact_cost = notional * (impact_bps / 10000.0)

        total_cash = (
            brokerage + stt + exch_fee + gst + sebi + stamp +
            spread_cost + slippage_cost + impact_cost
        )
        total_bps = (total_cash / notional) * 10000.0 if notional > 0 else 0.0

        return FrictionBreakdown(
            brokerage=round(brokerage, 2),
            stt_tax=round(stt, 2),
            exchange_txn_fee=round(exch_fee, 2),
            gst=round(gst, 2),
            sebi_charges=round(sebi, 2),
            stamp_duty=round(stamp, 2),
            bid_ask_spread_cost=round(spread_cost, 2),
            slippage_cost=round(slippage_cost, 2),
            market_impact_cost=round(impact_cost, 2),
            total_cost_cash=round(total_cash, 2),
            total_cost_bps=round(total_bps, 2)
        )
