import os, sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("", "common", "core", "api", "pipelines", "engines", "collectors", "intel"):
    _p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if _p not in sys.path:
        sys.path.insert(0, _p)
