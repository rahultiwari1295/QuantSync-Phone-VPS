"""
collectors/core/pipeline.py — validate-then-promote. The only code that writes to live tables.

Rules:
  * a row is promoted only if every rule passes;
  * a rejected row is logged (validation_log) and the old value stays;
  * suspicious-but-maybe-true rows (big jumps, PBT mismatch) go to review_queue and are held;
  * official filings (NSE/BSE) overwrite screener rows; screener never overwrites an official row.
"""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from . import db, validators as V
from .parsers import quarter_label

OFFICIAL = ("NSE", "BSE")


def is_official(source: Optional[str]) -> bool:
    return bool(source) and str(source).upper().startswith(OFFICIAL)


# ── job run tracking ────────────────────────────────────────────────────────
@dataclass
class JobRun:
    job: str
    trigger: str = "schedule"
    args: Optional[dict] = None
    seen: int = 0
    saved: int = 0
    rejected: int = 0
    status: str = "RUNNING"
    run_id: int = 0
    lines: List[str] = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.lines.append(line)
        self.lines = self.lines[-300:]
        print(line, flush=True)

    def __enter__(self) -> "JobRun":
        with db.session() as c:
            cur = c.execute("INSERT INTO job_runs(job,started_at,status,trigger,args) VALUES (?,?,?,?,?)",
                            (self.job, db.now_iso(), "RUNNING", self.trigger, json.dumps(self.args or {})))
            self.run_id = cur.lastrowid
        return self

    def checkpoint(self) -> None:
        with db.session() as c:
            c.execute("UPDATE job_runs SET rows_seen=?, rows_saved=?, rows_rejected=?, log_tail=? WHERE id=?",
                      (self.seen, self.saved, self.rejected, "\n".join(self.lines[-200:]), self.run_id))

    def __exit__(self, et, ev, tb) -> bool:
        if et is not None:
            self.status = "FAILED"
            self.log("EXCEPTION " + "".join(traceback.format_exception_only(et, ev)).strip())
            self.lines += traceback.format_exc().splitlines()[-12:]
        elif self.status == "RUNNING":
            self.status = "OK" if self.rejected == 0 else "PARTIAL"
        summary = f"{self.saved} saved, {self.rejected} rejected of {self.seen} in {time.time() - self.started:.0f}s"
        with db.session() as c:
            c.execute("UPDATE job_runs SET finished_at=?, status=?, rows_seen=?, rows_saved=?, rows_rejected=?, summary=?, log_tail=? WHERE id=?",
                      (db.now_iso(), self.status, self.seen, self.saved, self.rejected, summary,
                       "\n".join(self.lines[-200:]), self.run_id))
        return False   # never swallow exceptions: the runner needs the exit code


# ── freshness ───────────────────────────────────────────────────────────────
FRESH_DAYS = 135
STALE_DAYS = 400


def freshness_state(latest_end: Optional[date], today: date) -> str:
    if latest_end is None:
        return "MISSING"
    age = (today - latest_end).days
    return "FRESH" if age <= FRESH_DAYS else ("STALE" if age <= STALE_DAYS else "DEAD")


# ── listing ─────────────────────────────────────────────────────────────────
def ensure_master_columns(hist: sqlite3.Connection) -> None:
    cols = {r[1] for r in hist.execute("PRAGMA table_info(CompanyMaster)")}
    for name, typ in (("listing_date", "TEXT"), ("isin", "TEXT"), ("listing_status", "TEXT")):
        if name not in cols:
            hist.execute(f"ALTER TABLE CompanyMaster ADD COLUMN {name} {typ}")


def ensure_fundamentals_columns(hist: sqlite3.Connection) -> None:
    cols = {r[1] for r in hist.execute("PRAGMA table_info(CompanyFundamentalsHistory)")}
    for name, typ in (("basis", "TEXT"), ("basis_changed", "INTEGER")):
        if name not in cols:
            hist.execute(f"ALTER TABLE CompanyFundamentalsHistory ADD COLUMN {name} {typ}")



def promote_listing(conn, hist, rows: List[Dict[str, Any]], source: str, run: JobRun, universe: Optional[set] = None) -> None:
    ensure_master_columns(hist)
    now = db.now_iso()
    for r in rows:
        run.seen += 1
        sym = r["symbol"]
        if universe is not None and sym not in universe:
            continue
        ld = r.get("listing_date")
        if r.get("series") and r["series"] not in ("EQ", "BE", "BZ", "SM", "ST"):
            continue
        if ld is None:
            db.log_validation(conn, run.job, "listing", sym, None, "listing_date", "missing listing date", "WARN", source)
            run.rejected += 1
            continue
        if ld > date.today():
            db.log_validation(conn, run.job, "listing", sym, str(ld), "not_future", V.not_future(ld, date.today()) or "", "REJECT", source)
            run.rejected += 1
            continue
        conn.execute("INSERT OR REPLACE INTO listing(symbol,isin,name,series,listing_date,face_value,status,source,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sym, r.get("isin"), r.get("name"), r.get("series"), ld.isoformat(), r.get("face_value"), "ACTIVE", source, now))
        hist.execute("UPDATE CompanyMaster SET listing_date=?, isin=?, listing_status='ACTIVE' WHERE symbol=?", (ld.isoformat(), r.get("isin"), sym))
        run.saved += 1


def listing_dates(conn) -> Dict[str, date]:
    out = {}
    for r in conn.execute("SELECT symbol, listing_date FROM listing WHERE listing_date IS NOT NULL"):
        try:
            out[r["symbol"]] = date.fromisoformat(r["listing_date"])
        except ValueError:
            pass
    return out


# ── quarterly results ───────────────────────────────────────────────────────
_NUM_COLS = ("revenue_cr", "op_profit_cr", "opm_pct", "net_profit_cr", "eps")


def _stored(hist, symbol: str) -> Dict[str, Dict[str, Any]]:
    hist.row_factory = sqlite3.Row
    return {r["period_end_date"]: dict(r) for r in hist.execute(
        "SELECT * FROM CompanyFundamentalsHistory WHERE symbol=? AND period_type='QUARTERLY'", (symbol,))}


def _median(vals: List[float]) -> Optional[float]:
    vals = sorted(v for v in vals if v is not None)
    if len(vals) < 2:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


HOLDING_COMPANIES = {"MAHSCOOTER", "BAJAJHLDNG", "PILANIINVS", "KALYANIFRG", "SUMMITSEC", "SIL", "BFUTILITIE"}


def check_quarter(row: Dict[str, Any], listing: Optional[date], today: date, ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """ctx = {"rev_med": median revenue of up to 4 earlier quarters, "np_med": same for net profit}.
    Returns {"reject": [(rule, reason)], "hold": [(rule, reason)], "note": [...]}."""
    reject, hold, note = [], [], []
    end = row["period_end"]
    for rule, why in (("not_future", V.not_future(end, today)),
                      ("future_period", V.future_period(end, today)),
                      # a quarter that ended shortly before listing can appear in the prospectus-era filing
                      ("after_listing", V.after_listing(end, listing, grace_days=100))):
        if why:
            reject.append((rule, why))
    kind = row.get("source_kind")
    if kind == "screener":
        why = V.pl_arithmetic(row.get("revenue_cr"), row.get("expenses_cr"), row.get("op_profit_cr"))
        if why:
            reject.append(("pl_arithmetic", why))
        why = V.pbt_chain(row.get("op_profit_cr"), row.get("other_income_cr"), row.get("interest_cr"),
                          row.get("depreciation_cr"), row.get("pbt_cr"))
        if why:
            hold.append(("pbt_chain", why))
    elif kind == "nse":
        rev, exp, oi, pbt = row.get("revenue_cr"), row.get("expenses_cr"), row.get("other_income_cr"), row.get("pbt_cr")
        if None not in (rev, exp, oi, pbt):
            why = V.cross_source(rev + oi - exp, pbt, tol_pct=8.0)
            if why:
                hold.append(("income_less_expenses_vs_pbt", why))    # exceptional items: human check
    rev, npf = row.get("revenue_cr"), row.get("net_profit_cr")
    if rev is not None and rev < 0:
        note.append(("negative_revenue", f"revenue {rev} (fair-value loss at an investment company?)"))
    if ctx:
        if row.get("is_holding_company"):
            why = V.sudden_jump(ctx.get("rev_med"), rev)
            if why:
                note.append(("holding_co_seasonal_swing", f"holding company revenue swing {why} vs median {ctx['rev_med']:.0f} (exempt from HOLD)"))
        elif row.get("basis_changed"):
            why = V.sudden_jump(ctx.get("rev_med"), rev)
            if why:
                note.append(("basis_switch_jump", f"revenue swing {why} across basis switch vs median {ctx['rev_med']:.0f} (exempt from HOLD)"))
        else:
            why = V.sudden_jump(ctx.get("rev_med"), rev)
            if why:
                hold.append(("jump_revenue_cr", f"revenue {why} vs recent median {ctx['rev_med']:.0f}"))
            why = V.sudden_jump(ctx.get("np_med"), npf, factor=1e9)        # profit: only a large sign flip, ordinary swings are normal
            if why:
                hold.append(("jump_net_profit_cr", f"net profit {why} vs recent median {ctx['np_med']:.0f}"))
    if rev and rev > 0 and npf is not None and npf > rev * 1.0 and npf > 50:
        note.append(("profit_exceeds_revenue", f"net profit {npf:.0f} above revenue {rev:.0f} (holding company / one-off gain?)"))
    return {"reject": reject, "hold": hold, "note": note}


def _write_quarter(hist, symbol: str, row: Dict[str, Any], source_tag: str, source_url: str, fetched_at: str, existing: Optional[Dict[str, Any]]) -> None:
    end = row["period_end"].isoformat()
    vals = {c: row.get(c) for c in _NUM_COLS}
    vals["interest_expense_cr"] = row.get("interest_cr")
    basis = row.get("basis") or ("consolidated" if row.get("consolidated") else "standalone")
    basis_changed = 1 if row.get("basis_changed") else 0
    if existing:
        sets = ", ".join(f"{k}=COALESCE(?, {k})" for k in vals) + ", source=?, source_url=?, source_fetched_at=?, quarter=?, basis=?, basis_changed=?"
        hist.execute(f"UPDATE CompanyFundamentalsHistory SET {sets} WHERE symbol=? AND period_end_date=? AND period_type='QUARTERLY'",
                     (*vals.values(), source_tag, source_url, fetched_at, quarter_label(row["period_end"]), basis, basis_changed, symbol, end))
    else:
        cols = ["symbol", "quarter", "period_type", "period_end_date", "reported_date", "available_date", "source",
                "source_url", "source_fetched_at", "basis", "basis_changed", *vals.keys()]
        reported = row.get("reported_date")
        hist.execute(f"INSERT OR IGNORE INTO CompanyFundamentalsHistory({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     (symbol, quarter_label(row["period_end"]), "QUARTERLY", end, reported, reported, source_tag, source_url,
                      fetched_at, basis, basis_changed, *vals.values()))


def promote_quarters(conn, hist, run: JobRun, symbol: str, rows: List[Dict[str, Any]], source_tag: str, source_url: str,
                     listing: Optional[date], today: Optional[date] = None, source_kind: str = "screener") -> int:
    ensure_fundamentals_columns(hist)
    today = today or date.today()
    stored = _stored(hist, symbol)
    seq = {d: {"revenue_cr": r.get("revenue_cr"), "net_profit_cr": r.get("net_profit_cr")} for d, r in stored.items()}   # includes held rows: a spike must not flag the quarters after it
    stored_dates = sorted(stored)
    fetched_at = db.now_iso()
    saved = 0
    for row in sorted(rows, key=lambda r: r["period_end"]):
        run.seen += 1
        basis = "consolidated" if row.get("consolidated") else "standalone"
        end_s = row["period_end"].isoformat()
        existing = stored.get(end_s)
        earlier = [d for d in sorted(seq) if d < end_s][-4:]
        prev = stored[earlier[-1]] if earlier and earlier[-1] in stored else None
        basis_changed = 1 if (prev and prev.get("basis") and prev.get("basis") != basis) else 0
        row = dict(row, source_kind=source_kind, basis=basis, basis_changed=basis_changed,
                   is_holding_company=(symbol in HOLDING_COMPANIES))
        seq[end_s] = {"revenue_cr": row.get("revenue_cr"), "net_profit_cr": row.get("net_profit_cr")}
        if existing and is_official(existing.get("source")) and not is_official(source_tag):
            continue                                                    # screener never overwrites an official filing
        ctx = {"rev_med": _median([seq[d].get("revenue_cr") for d in earlier]),
               "np_med": _median([seq[d].get("net_profit_cr") for d in earlier])} if len(earlier) >= 2 else None
        prev = stored[earlier[-1]] if earlier and earlier[-1] in stored else None
        verdict = check_quarter(row, listing, today, ctx)
        for rule, w in verdict["note"]:
            db.log_validation(conn, run.job, "results", symbol, end_s, rule, w, "INFO", source_tag)
        if verdict["reject"]:
            run.rejected += 1
            for rule, why in verdict["reject"]:
                db.log_validation(conn, run.job, "results", symbol, end_s, rule, why, "REJECT", source_tag, row)
            continue
        if verdict["hold"] and not (existing and existing.get("revenue_cr") == row.get("revenue_cr")
                                    and existing.get("net_profit_cr") == row.get("net_profit_cr")):
            run.rejected += 1
            why = "; ".join(w for _, w in verdict["hold"])
            for rule, w in verdict["hold"]:
                db.log_validation(conn, run.job, "results", symbol, end_s, rule, w, "HOLD", source_tag, row)
            db.queue_review(conn, "results", symbol, end_s, why, existing or prev, {**row, "_source_tag": source_tag, "_source_url": source_url},
                            source=source_tag)
            continue
        if existing and is_official(source_tag) and not is_official(existing.get("source")):
            for k in ("revenue_cr", "net_profit_cr"):
                why = V.cross_source(existing.get(k), row.get(k))
                if why:
                    db.log_validation(conn, run.job, "results", symbol, end_s, f"screener_vs_official_{k}",
                                      why + " (official filing kept)", "INFO", source_tag)
        _write_quarter(hist, symbol, row, source_tag, source_url, fetched_at, existing)
        stored[end_s] = {"period_end_date": end_s, "source": source_tag, **{c: row.get(c) for c in _NUM_COLS}}
        stored_dates = sorted(stored)
        saved += 1
        run.saved += 1
    return saved


def approve_review(review_id: int, who: str) -> Dict[str, Any]:
    """Promote a held row after a human approved it on the ops page."""
    with db.session() as c, sqlite3.connect(db.HIST_DB, timeout=30) as hist:
        r = c.execute("SELECT * FROM review_queue WHERE id=? AND status='PENDING'", (review_id,)).fetchone()
        if not r:
            return {"ok": False, "error": "not pending"}
        new = json.loads(r["new_json"])
        if r["dataset"] == "results" and new.get("period_end"):
            new["period_end"] = date.fromisoformat(str(new["period_end"])[:10])
            existing = _stored(hist, r["symbol"]).get(new["period_end"].isoformat())
            _write_quarter(hist, r["symbol"], new, new.get("_source_tag") or "MANUAL_APPROVED", new.get("_source_url") or "",
                           db.now_iso(), existing)
        c.execute("UPDATE review_queue SET status='APPROVED', decided_at=?, decided_by=? WHERE id=?", (db.now_iso(), who, review_id))
        db.audit(c, who, "review_approve", {"id": review_id, "symbol": r["symbol"], "period": r["period"]})
    return {"ok": True}


def reject_review(review_id: int, who: str) -> Dict[str, Any]:
    with db.session() as c:
        n = c.execute("UPDATE review_queue SET status='REJECTED', decided_at=?, decided_by=? WHERE id=? AND status='PENDING'",
                      (db.now_iso(), who, review_id)).rowcount
        db.audit(c, who, "review_reject", {"id": review_id})
    return {"ok": bool(n)}


# ── corporate actions / dividends ───────────────────────────────────────────
def promote_actions(conn, hist, run: JobRun, rows: List[Dict[str, Any]], listing: Dict[str, date], source: str) -> None:
    now = db.now_iso()
    for r in rows:
        run.seen += 1
        sym, ex = r["symbol"], r["ex_date"]
        ld = listing.get(sym)
        why = V.after_listing(ex, ld, grace_days=5)
        if why:
            db.log_validation(conn, run.job, "corp_actions", sym, str(ex), "after_listing", why, "REJECT", source, r)
            run.rejected += 1
            continue
        if ex > date.today() + timedelta(days=120):
            db.log_validation(conn, run.job, "corp_actions", sym, str(ex), "far_future", f"ex-date {ex} too far ahead", "REJECT", source, r)
            run.rejected += 1
            continue
        if r["kind"] == "DIVIDEND":
            amt = r.get("amount")
            fv = r.get("face_value")
            if amt is None or amt <= 0 or amt > 5000:
                db.log_validation(conn, run.job, "corp_actions", sym, str(ex), "dividend_amount", f"bad amount {amt}", "REJECT", source, r)
                run.rejected += 1
                continue
            if fv and fv > 0 and amt > fv * 200:
                db.log_validation(conn, run.job, "corp_actions", sym, str(ex), "dividend_vs_face", f"amount {amt} vs face {fv}", "HOLD", source, r)
                db.queue_review(conn, "corp_actions", sym, str(ex), f"dividend {amt} is >200x face value {fv}", None, r, source=source)
                run.rejected += 1
                continue
            conn.execute("INSERT OR REPLACE INTO dividends_clean(symbol,ex_date,amount,subject,source,updated_at) VALUES (?,?,?,?,?,?)",
                         (sym, ex.isoformat(), amt, r.get("subject"), source, now))
        sid = f"{sym}|{ex.isoformat()}|{r['kind']}|{r.get('amount')}"
        hist.execute("INSERT OR IGNORE INTO CorporateEvents(symbol,event_date,event_type,ex_date,record_date,amount,source_event_id,source,details_json) VALUES (?,?,?,?,?,?,?,?,?)",
                     (sym, ex.isoformat(), r["kind"], ex.isoformat(), r["record_date"].isoformat() if r.get("record_date") else None,
                      r.get("amount"), sid, source, json.dumps(r, default=str)))
        run.saved += 1


# ── announcements / board meetings ──────────────────────────────────────────
def promote_announcements(conn, run: JobRun, rows: List[Dict[str, Any]], source: str, known: Optional[set] = None) -> None:
    now = db.now_iso()
    for r in rows:
        run.seen += 1
        if known is not None and r["symbol"] not in known:
            continue
        try:
            ts = datetime.fromisoformat(r["ts"])
        except ValueError:
            run.rejected += 1
            continue
        if ts > datetime.now() + timedelta(hours=2):
            db.log_validation(conn, run.job, "announcements", r["symbol"], r["ts"], "not_future", "timestamp in the future", "REJECT", source)
            run.rejected += 1
            continue
        cur = conn.execute("INSERT OR IGNORE INTO announcements(hash,symbol,isin,ts,headline,detail,url,category,source,source_id,fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (r["hash"], r["symbol"], r["isin"], r["ts"], r["headline"], r["detail"], r["url"], r["category"], source, r["source_id"], now))
        run.saved += cur.rowcount


def promote_meetings(conn, hist, run: JobRun, rows: List[Dict[str, Any]], source: str, known: Optional[set] = None) -> None:
    now = db.now_iso()
    for r in rows:
        run.seen += 1
        if known is not None and r["symbol"] not in known:
            continue
        conn.execute("INSERT OR REPLACE INTO board_meetings(symbol,meeting_date,purpose,detail,isin,is_results,source,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                     (r["symbol"], r["meeting_date"].isoformat(), r["purpose"], r["detail"], r["isin"], 1 if r["is_results"] else 0, source, now))
        if r["is_results"] and r["meeting_date"] >= date.today():
            hist.execute("UPDATE CompanyMaster SET next_results_date=? WHERE symbol=? AND (next_results_date IS NULL OR next_results_date<? OR next_results_date>?)",
                         (r["meeting_date"].isoformat(), r["symbol"], date.today().isoformat(), r["meeting_date"].isoformat()))
        run.saved += 1
