"""
QuantSync-Lines: Core Module Exports
====================================
"""

from core.weekly_aggregator import aggregate_to_weekly, load_universe_weekly
from core.fibonacci_calculator import (
    calculate_trend_fibonacci,
    compute_level_distance,
    compute_extension_position,
    FibProjection,
    DEFAULT_FIB_RATIOS
)
from core.swing_detector import SwingDetector, SwingPoint, FibStructure
from core.signal_detector import SignalDetector, BreakoutEvent, RejectionEvent
from core.order_models import (
    build_order_setup,
    compute_weekly_atr,
    OrderSetup,
    EntrySpec,
    StopSpec,
    TargetSpec
)
from core.features import compute_fibonacci_features, FIBONACCI_FEATURE_COLUMNS

__all__ = [
    "aggregate_to_weekly",
    "load_universe_weekly",
    "calculate_trend_fibonacci",
    "compute_level_distance",
    "compute_extension_position",
    "FibProjection",
    "DEFAULT_FIB_RATIOS",
    "SwingDetector",
    "SwingPoint",
    "FibStructure",
    "SignalDetector",
    "BreakoutEvent",
    "RejectionEvent",
    "build_order_setup",
    "compute_weekly_atr",
    "OrderSetup",
    "EntrySpec",
    "StopSpec",
    "TargetSpec",
    "compute_fibonacci_features",
    "FIBONACCI_FEATURE_COLUMNS",
]
