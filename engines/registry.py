"""
engines/registry.py — Dynamic Engine Auto-Discovery & Registry
==============================================================
Automatically discovers, validates, and instantiates all engine plugins
located in Prod_VPS/engines/*/engine.py without requiring any code changes to scheduler.
"""

import os
import sys
import json
import importlib
import logging
from typing import Dict, List, Any, Optional

logger = logging.getLogger("ProdVPS.Registry")

_ENGINES_DIR = os.path.dirname(os.path.abspath(__file__))
_PROD_ROOT = os.path.dirname(_ENGINES_DIR)
if _PROD_ROOT not in sys.path:
    sys.path.insert(0, _PROD_ROOT)

from engines.base_engine import BaseTradingEngine


def load_config() -> Dict[str, Any]:
    cfg_path = os.path.join(_PROD_ROOT, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"engines": {}}


class EngineRegistry:
    """
    Dynamic plugin registry that auto-discovers trading engines in Prod_VPS.
    """
    def __init__(self, engines_dir: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        self.engines_dir = engines_dir or _ENGINES_DIR
        self.config = config or load_config()
        self._loaded_engines: Dict[str, BaseTradingEngine] = {}

    def discover_and_load(self) -> Dict[str, BaseTradingEngine]:
        """
        Scans engines directory for any subfolder with engine.py.
        Instantiates each engine if enabled in config.json.
        """
        self._loaded_engines.clear()
        engines_cfg = self.config.get("engines", {})

        for item in sorted(os.listdir(self.engines_dir)):
            sub_path = os.path.join(self.engines_dir, item)
            if not os.path.isdir(sub_path) or item.startswith(("_", ".")) or item.endswith(("_old", "_bak", "_backup")):
                continue

            engine_file = os.path.join(sub_path, "engine.py")
            if not os.path.exists(engine_file):
                continue

            eng_id = item.lower()
            eng_cfg = engines_cfg.get(eng_id, {"enabled": True, "name": f"QuantSync-{item.upper()}"})

            if not eng_cfg.get("enabled", True):
                logger.info(f"Engine [{eng_id}] is disabled in config.json — skipping.")
                continue

            # Dynamic import
            module_name = f"engines.{eng_id}.engine"
            try:
                if module_name in sys.modules:
                    mod = importlib.reload(sys.modules[module_name])
                else:
                    mod = importlib.import_module(module_name)

                if hasattr(mod, "get_engine"):
                    engine_instance = mod.get_engine(eng_cfg)
                elif hasattr(mod, "TradingEnginePlugin"):
                    engine_instance = mod.TradingEnginePlugin(eng_id=eng_id, config=eng_cfg)
                else:
                    logger.error(f"Module {module_name} has no get_engine() or TradingEnginePlugin class.")
                    continue

                self._loaded_engines[eng_id] = engine_instance
                logger.info(f"Successfully loaded engine plugin: [{eng_id}] -> {engine_instance.engine_name}")

            except Exception as e:
                logger.error(f"Failed to load engine [{eng_id}]: {e}", exc_info=True)

        return self._loaded_engines

    def get_engine(self, engine_id: str) -> Optional[BaseTradingEngine]:
        if not self._loaded_engines:
            self.discover_and_load()
        return self._loaded_engines.get(engine_id.lower())

    def get_all_engines(self) -> Dict[str, BaseTradingEngine]:
        if not self._loaded_engines:
            self.discover_and_load()
        return self._loaded_engines
