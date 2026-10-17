"""Morning (07:00) and evening (17:00) briefs, stored as JSON in intel.db and sent to Telegram.

Morning: world close, overnight filings and news ranked by impact, today's calendar, yesterday's flows and deals.
Evening: today's market, the day's top filings, insider and bulk/block activity, tomorrow's calendar."""
from __future__ import annotations

import html
import time
from datetime import datetime, timedelta

from . import db

TOP = 12


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _ev(r: dict) -> dict:
    return {k: r.get(k) for k in ("id", "symbol", "company", "ts", "type", "category", "source", "url", "headline", "summary", "sentiment", "impact")}


def build(kind: str = "morning") -> dict:
    now = db.now_ist().replace(tzinfo=None)
    day = now.strftime("%Y-%m-%d")
    since = (now - timedelta(hours=16 if kind == "morning" else 10)).strftime("%Y-%m-%dT%H:%M:%S")
    nxt = (now + timedelta(days=1 if kind == "evening" else 0)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    with db.session() as con:
        markets = _rows(con, "SELECT key,name,region,last,chg_pct,as_of FROM markets ORDER BY region, name")
        top = _rows(con, "SELECT * FROM events WHERE ts>=? AND category NOT IN ('global','macro') AND symbol IS NOT NULL "
                         "ORDER BY impact DESC, ts DESC LIMIT ?", (since, TOP))
        world = _rows(con, "SELECT * FROM events WHERE ts>=? AND category IN ('global','macro') ORDER BY impact DESC, ts DESC LIMIT 8", (since,))
        red = _rows(con, "SELECT * FROM events WHERE ts>=? AND sentiment='negative' AND impact>=50 ORDER BY impact DESC LIMIT 6", (since,))
        insiders = _rows(con, "SELECT symbol, person, category, side, value_cr, mode, known_at FROM insider_trades "
                              "WHERE known_at>=date(?,'-2 day') ORDER BY value_cr DESC LIMIT 10", (day,))
        deals = _rows(con, "SELECT symbol, party, side, qty, price, value_cr, kind, deal_date FROM deals "
                           "WHERE deal_date>=date(?,'-3 day') ORDER BY value_cr DESC LIMIT 12", (day,))
        flows = _rows(con, "SELECT day, who, buy_cr, sell_cr, net_cr FROM flows ORDER BY day DESC LIMIT 4")
        cal = _rows(con, "SELECT symbol, meeting_date, purpose, is_results FROM meetings WHERE meeting_date BETWEEN ? AND ? "
                         "ORDER BY meeting_date, is_results DESC LIMIT 60", (nxt, end))
        stats = dict(con.execute("SELECT count(*) n, sum(impact>=70) hi FROM events WHERE ts>=?", (since,)).fetchone())
        body = {"kind": kind, "day": day, "made_at": now.strftime("%Y-%m-%d %H:%M"), "since": since, "stats": stats,
                "markets": markets, "top": [_ev(r) for r in top], "world": [_ev(r) for r in world], "red_flags": [_ev(r) for r in red],
                "insiders": insiders, "deals": deals, "flows": flows, "calendar": cal}
        bid = f"{kind}-{day}"
        con.execute("INSERT OR REPLACE INTO briefs(id,kind,day,created_at,body_json) VALUES(?,?,?,?,?)", (bid, kind, day, time.time(), db.jdump(body)))
    return body


def telegram_text(b: dict) -> str:
    e = html.escape
    title = "Morning brief" if b["kind"] == "morning" else "Evening brief"
    out = [f"<b>QuantSync {title}</b> · {b['day']}"]
    mk = [m for m in b["markets"] if m.get("chg_pct") is not None][:8]
    if mk:
        out.append("\n<b>Markets</b>\n" + "\n".join(f"{e(m['name'])}: {m['chg_pct']:+.2f}%" for m in mk))
    if b["top"]:
        out.append("\n<b>What matters</b>")
        for x in b["top"][:8]:
            out.append(f"• <b>{e(x['symbol'] or '')}</b> [{x['impact']}] {e((x['summary'] or x['headline']).splitlines()[0][:160])}")
    if b["red_flags"]:
        out.append("\n<b>Red flags</b>\n" + "\n".join(f"• <b>{e(x['symbol'] or '')}</b> {e(x['headline'][:120])}" for x in b["red_flags"][:4]))
    if b["insiders"]:
        out.append("\n<b>Insiders</b>\n" + "\n".join(f"• {e(i['symbol'])}: {e(i['person'][:40])} {i['side'].lower()} ₹{(i['value_cr'] or 0):.2f} cr"
                                                      for i in b["insiders"][:5]))
    if b["calendar"]:
        res = [c for c in b["calendar"] if c["is_results"]][:10]
        if res:
            out.append("\n<b>Results coming</b>\n" + ", ".join(f"{e(c['symbol'])} ({datetime.strptime(c['meeting_date'], '%Y-%m-%d'):%d %b})" for c in res))
    out.append("\nNot investment advice. Facts from exchange filings and news.")
    return "\n".join(out)[:4000]


def send(b: dict) -> bool:
    try:
        import notifier  # the engine's Telegram sender (VPS root on sys.path)
        return bool(notifier.send_message(telegram_text(b)))
    except Exception:  # noqa: BLE001 - a brief must never fail because Telegram did
        return False


def run(kind: str, log=print) -> dict:
    t0 = time.time()
    b = build(kind)
    sent = send(b)
    with db.session() as con:
        db.record_run(con, f"brief:{kind}", t0, True, len(b["top"]), f"sent={sent}")
    log(f"{kind} brief: {len(b['top'])} top items, {len(b['calendar'])} calendar rows, telegram={sent}")
    return {"kind": kind, "items": len(b["top"]), "sent": sent}
