"""Rule JSON v1: validate -> normalise -> hash -> compile to parameterised, whitelist-only SQL."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import FSV
from .catalog import BOOL_KEYS, FEATURE_KEYS, STRING_KEYS

MAX_CONDITIONS, MAX_DEPTH, MAX_LAG = 12, 3, 20
CMP = {">", ">=", "<", "<=", "==", "!="}
OPS = CMP | {"between", "crosses_above", "crosses_below", "is_true", "in"}


class RuleError(ValueError):
    pass


def _num(x, where):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or abs(x) > 1e12:
        raise RuleError(f"{where}: expected a finite number")
    return float(x)


def _operand(o, where, allow_str=False):
    if not isinstance(o, dict) or len(o) not in (1, 2):
        raise RuleError(f"{where}: bad operand")
    if "f" in o:
        f = o["f"]
        ok = f in FEATURE_KEYS or (allow_str and f in STRING_KEYS)
        if not ok:
            raise RuleError(f"{where}: unknown field")
        lag = o.get("lag", 0)
        if not isinstance(lag, int) or isinstance(lag, bool) or not 0 <= lag <= MAX_LAG:
            raise RuleError(f"{where}: lag must be 0..{MAX_LAG}")
        if set(o) - {"f", "lag"}:
            raise RuleError(f"{where}: bad operand")
        return {"f": f, **({"lag": lag} if lag else {})}
    if "c" in o and len(o) == 1:
        return {"c": _num(o["c"], where)}
    if "list" in o and len(o) == 1:
        v = o["list"]
        if not isinstance(v, list) or not 1 <= len(v) <= 50:
            raise RuleError(f"{where}: list must have 1..50 items")
        if all(isinstance(i, str) for i in v):
            if any(not i.replace("&", "").replace("-", "").isalnum() or len(i) > 20 for i in v):
                raise RuleError(f"{where}: bad symbol")
            return {"list": sorted(set(v))}
        return {"list": [_num(i, where) for i in v]}
    raise RuleError(f"{where}: bad operand")


def _cond(c, where):
    if not isinstance(c, dict) or set(c) - {"l", "op", "r"} or c.get("op") not in OPS:
        raise RuleError(f"{where}: bad condition")
    op = c["op"]
    l = _operand(c.get("l"), where, allow_str=(op == "in"))
    if op == "is_true":
        if "f" not in l or l["f"] not in BOOL_KEYS or "r" in c:
            raise RuleError(f"{where}: is_true needs a yes/no field")
        return {"l": l, "op": op}
    r = _operand(c.get("r"), where, allow_str=False)
    if op == "between":
        if "list" not in r or len(r["list"]) != 2 or not all(isinstance(i, float) for i in r["list"]):
            raise RuleError(f"{where}: between needs two numbers")
        r = {"list": sorted(r["list"])}
    elif op == "in":
        if l.get("f") != "symbol" or "list" not in r or not all(isinstance(i, str) for i in r["list"]):
            raise RuleError(f"{where}: 'in' is only for symbol lists")
    else:
        if "list" in r:
            raise RuleError(f"{where}: list not allowed here")
        if l.get("f") == "symbol":
            raise RuleError(f"{where}: symbol only supports 'in'")
    return {"l": l, "op": op, "r": r}


def _group(g, depth, counter, where):
    if depth > MAX_DEPTH:
        raise RuleError("rule is nested too deeply")
    if not isinstance(g, dict) or len(g) != 1 or next(iter(g)) not in ("all", "any"):
        raise RuleError(f"{where}: group must be all/any")
    kind, items = next(iter(g.items()))
    if not isinstance(items, list) or not items:
        raise RuleError(f"{where}: empty group")
    out = []
    for i, it in enumerate(items):
        w = f"{where}.{kind}[{i}]"
        if isinstance(it, dict) and ("all" in it or "any" in it):
            out.append(_group(it, depth + 1, counter, w))
        else:
            counter[0] += 1
            if counter[0] > MAX_CONDITIONS:
                raise RuleError(f"at most {MAX_CONDITIONS} conditions")
            out.append(_cond(it, w))
    return {kind: out}


def normalise(rule: dict) -> dict:
    if not isinstance(rule, dict) or rule.get("v") != 1:
        raise RuleError("rule version must be 1")
    body = {k: v for k, v in rule.items() if k != "v"}
    return {"v": 1, **_group(body, 1, [0], "rule")}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def rule_hash(rule: dict, tf: str) -> str:
    return hashlib.sha256(f"{canonical(normalise(rule))}|{tf}|{FSV}".encode()).hexdigest()[:24]


@dataclass
class Compiled:
    where: str
    params: list = field(default_factory=list)
    lags: set = field(default_factory=set)  # {(feature, n)}


def lag_col(f: str, n: int) -> str:
    return f'"{f}__l{n}"'


def _expr(o, c: Compiled, extra=0):
    if "c" in o:
        c.params.append(o["c"])
        return "?"
    n = o.get("lag", 0) + extra
    if n:
        c.lags.add((o["f"], n))
        return lag_col(o["f"], n)
    return f'"{o["f"]}"'


def _sql_cond(cd, c: Compiled):
    op, l = cd["op"], cd["l"]
    if op == "is_true":
        return f"({_expr(l, c)} = 1)"
    if op == "in":
        c.params.extend(cd["r"]["list"])
        return f'("symbol" IN ({",".join("?" * len(cd["r"]["list"]))}))'
    if op == "between":
        lo, hi = cd["r"]["list"]
        e = _expr(l, c)
        c.params.extend([lo, hi])
        return f"({e} BETWEEN ? AND ?)"
    if op in CMP:
        a = _expr(l, c); b = _expr(cd["r"], c)
        sql_op = "=" if op == "==" else "<>" if op == "!=" else op
        return f"({a} {sql_op} {b})"
    a0, b0 = _expr(l, c), _expr(cd["r"], c)
    a1, b1 = _expr(l, c, 1), _expr(cd["r"], c, 1)
    # parameter order must follow placeholder order in the final string
    if op == "crosses_above":
        return f"({a0} > {b0} AND {a1} <= {b1})"
    return f"({a0} < {b0} AND {a1} >= {b1})"


def _sql_group(g, c):
    kind, items = next(iter(g.items()))
    parts = [_sql_group(i, c) if ("all" in i or "any" in i) else _sql_cond(i, c) for i in items]
    return "(" + (" AND " if kind == "all" else " OR ").join(parts) + ")"


def compile_rule(rule: dict) -> Compiled:
    c = Compiled(where="")
    c.where = _sql_group({k: v for k, v in normalise(rule).items() if k != "v"}, c)
    return c


def lag_select(c: Compiled) -> str:
    return "".join(
        f', LAG("{f}", {n}) OVER (PARTITION BY "symbol" ORDER BY "date") AS {lag_col(f, n)}' for f, n in sorted(c.lags))


# ---- exits -----------------------------------------------------------------------------------
EXIT_PARAM = {"stop_pct": (0.1, 50), "target_pct": (0.1, 200), "atr_stop": (0.1, 20), "atr_target": (0.1, 40),
              "trail_atr": (0.1, 20), "trail_pct": (0.1, 50), "time": (1, 250)}


def normalise_exit(exit_: dict | None) -> dict:
    """Exit JSON v1: {"v":1,"any":[{"type":"stop_pct","v":5},{"type":"rule","rule":{...}}]}"""
    if exit_ is None:
        return {"v": 1, "any": []}
    if not isinstance(exit_, dict) or exit_.get("v") != 1 or set(exit_) - {"v", "any"}:
        raise RuleError("exit version must be 1")
    items, seen, out = exit_.get("any", []), set(), []
    if not isinstance(items, list) or len(items) > 8:
        raise RuleError("at most 8 exit conditions")
    for it in items:
        t = it.get("type") if isinstance(it, dict) else None
        if t == "rule":
            if "rule" not in it or set(it) - {"type", "rule"}:
                raise RuleError("exit rule malformed")
            out.append({"type": "rule", "rule": normalise(it["rule"])})
        elif t in EXIT_PARAM:
            lo, hi = EXIT_PARAM[t]
            if set(it) - {"type", "v"}:
                raise RuleError("exit malformed")
            v = _num(it.get("v"), t)
            if not lo <= v <= hi:
                raise RuleError(f"{t} must be {lo}..{hi}")
            out.append({"type": t, "v": int(v) if t == "time" else v})
        else:
            raise RuleError("unknown exit type")
        if t in seen:
            raise RuleError(f"duplicate exit: {t}")
        seen.add(t)
    return {"v": 1, "any": sorted(out, key=canonical)}
