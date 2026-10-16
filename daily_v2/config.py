"""
QuantSync Daily V2 Configuration Module
---------------------------------------
Centralizes all frozen parameters, paths, thresholds, and health rules for Daily V2.
"""

import os

DAILY_V2_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(DAILY_V2_DIR)

# System Version & Specification
DAILY_V2_VERSION = "DAILY_V2_GATE_2B_FROZEN_2026_08_30"
SPECIFICATION_VERSION = "DAILY_V2_GATE_2B_FROZEN_2026_08_30"
EXECUTION_CONVENTION = "SAME_DAY_CLOSE"

# Model Paths & Metadata
MODELS_DIR = os.path.join(DAILY_V2_DIR, "models")
FROZEN_MODEL_PATH = os.path.join(MODELS_DIR, "frozen_daily_v2_model.json")
FEATURE_NAMES_PATH = os.path.join(MODELS_DIR, "feature_names.json")
MANIFEST_PATH = os.path.join(MODELS_DIR, "manifest.json")
FINAL_CONFIG_PATH = os.path.join(MODELS_DIR, "FINAL_CONFIGURATION.json")
SNAPSHOT_LOCK_PATH = os.path.join(MODELS_DIR, "SNAPSHOT_LOCK.md")

# Core ML Decision Threshold & Gate 2B Rules
DAILY_V2_ML_THRESHOLD = 0.60
GATE_2B_PLUS_DI_MINUS_DI_FILTER = True
GATE_2B_MIN_VOL_RATIO_20D = 1.20
DAILY_CAPACITY_LIMIT = 3  # Daily Top 3 Max candidates per day

# Active Production Strategies (9 Surviving Strategies; HyperCoil is isolated/disabled)
ACTIVE_STRATEGIES = [
    "Breakout",
    "Alpha",
    "NR7",
    "CarterSqueeze",
    "Decouple",
    "Snapback",
    "TurtleSoup",
    "DipBuyer",
    "WyckoffSpring"
]

DISABLED_STRATEGIES = [
    "HyperCoil"  # Retained in research; excluded from production deployment
]

# Negation Rules (Failure Prediction Filters)
NEGATION_RULES = {
    "MAX_ATR_PCT": 5.5,             # Reject if atr14_pct > 5.5% (hyper-volatility churn)
    "MAX_DIST_52W_HIGH_PCT": 20.0,  # Reject if dist_52w_high_pct > 20% (overhead supply)
    "REQUIRE_NIFTY_EMA50": False    # Disabled: ML probability already embeds regime; binary cutoff hurts pullbacks/springs
}

# Trade Health Engine Parameters
HEALTH_CONFIG = {
    "HARD_STOP_LOSS_PCT": -8.0,     # Terminal stop loss for any position
    "EARLY_WARNING_MAE_PCT": -5.0,  # Warning state trigger: 5-day drawdown <= -5.0% without upside
    "WARNING_RSI_THRESHOLD": 48.0,  # Warning state trigger: RSI14 drops below 48
    "MFE_EXPANSION_HURDLE": 15.0,   # Upside expansion hurdle to activate trailing profit protection
    "MFE_TRAIL_CAPTURE_PCT": 0.75,  # Capture 75% of peak MFE on compounders
    "MODERATE_WINNER_TRAIL": 0.60,  # Capture 60% of peak MFE on moderate winners (MFE >= 15%)
    "EMA20_PROXIMITY_PCT": 0.0      # Close below 20 EMA triggers warning monitor
}

# Persistence & Storage Configuration
DATABASE_PATH = os.path.join(DAILY_V2_DIR, "daily_v2_production.db")
LOGS_DIR = os.path.join(DAILY_V2_DIR, "logs")

# Execution & Safety
DEFAULT_DRY_RUN = True              # Dry-run mode by default for shadow verification
ROUND_TRIP_FRICTION_PCT = 0.15      # 0.15% brokerage, STT, exchange, slippage

# Strategy Display Names for Wix UI & Telegram
STRATEGY_DISPLAY_NAMES = {
    "Breakout": "Breakout 52W High",
    "Alpha": "Alpha Relative Strength",
    "NR7": "NR7 Range Contraction",
    "CarterSqueeze": "Carter Squeeze",
    "Decouple": "Decouple Relative Strength",
    "Snapback": "Snapback Mean Reversion",
    "TurtleSoup": "Turtle Soup 20D",
    "DipBuyer": "Dip Buyer",
    "WyckoffSpring": "Wyckoff Spring",
    "HyperCoil": "Hyper Coil"
}

# Telegram Configuration
def _load_tg_config():
    import json
    cfg = {
        "enabled": True,
        "debug_notifications": True,
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "channel_id": os.environ.get("TELEGRAM_CHANNEL_ID", "")
    }
    cfg_file = os.path.join(PROJECT_ROOT, "telegram_config.json")
    if os.path.exists(cfg_file):
        try:
            with open(cfg_file, "r") as f:
                disk_cfg = json.load(f)
                cfg.update(disk_cfg)
        except Exception:
            pass
    return cfg

TELEGRAM_CONFIG = _load_tg_config()


