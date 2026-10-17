"""Rule-based event typing, sentiment and a two-line plain summary. Cheap, exact, explainable; the model only reads what
these rules mark as worth reading. Order matters: the first matching rule wins."""
from __future__ import annotations

import re

# type, label, base impact, sentiment, pattern (searched in "category | headline | detail", lower case)
RULES: list[tuple[str, str, int, str, str]] = [
    ("auditor_exit", "Auditor exit", 70, "negative", r"resignation of (the )?(statutory |secretarial )?auditor|auditor.{0,30}resign"),
    ("default", "Default / delay", 80, "negative", r"\bdefault\b|delay in (payment|servicing)|non[- ]payment of (interest|principal)"),
    ("insolvency", "Insolvency", 85, "negative", r"insolvency|\bibc\b|\bnclt\b.{0,40}(admit|initiat)|corporate insolvency"),
    ("sebi_order", "Regulator action", 65, "negative", r"\bsebi\b.{0,40}(order|penalty|show cause)|show cause notice|adjudication order"),
    ("fraud", "Fraud / forensic", 85, "negative", r"\bfraud\b|forensic audit|misappropriation|search (and|&) seizure|income tax (search|survey)"),
    ("kmp_exit", "Top management exit", 55, "negative", r"resignation of (the )?(chief|managing director|\bmd\b|\bceo\b|\bcfo\b|whole[- ]time director|company secretary)|(ceo|cfo|managing director).{0,20}resign"),
    ("rating_downgrade", "Rating downgrade", 60, "negative", r"downgrad|rating.{0,40}(revised|lowered) (to|from).{0,30}(negative|watch)|credit watch with negative"),
    ("rating_upgrade", "Rating upgrade", 50, "positive", r"upgrad.{0,40}rating|rating.{0,40}upgrad|outlook.{0,20}(revised to )?positive"),
    ("pledge_create", "Pledge created", 45, "negative", r"creation of (pledge|encumbrance)|pledge(d)? .{0,30}created|invocation of pledge"),
    ("pledge_release", "Pledge released", 40, "positive", r"release of (pledge|encumbrance)|revocation of pledge"),
    ("results_date", "Results date", 20, "neutral", r"board meeting.{0,60}intimation|intimation.{0,60}board meeting|(to consider|will be held|is scheduled|proposes to).{0,120}result|prior intimation"),
    ("results", "Results", 55, "neutral", r"financial results|quarterly results|audited results|unaudited.{0,20}results|outcome of board meeting.{0,80}results"),
    ("order_win", "Order win", 50, "positive", r"\border(s)?\b.{0,40}(received|win|won|bagged|secured|awarded|worth)|letter of (award|intent)|\bloa\b|\bloi\b|bags? .{0,20}order|contract (award|worth)"),
    ("fund_raise", "Fund raise", 45, "neutral", r"\bqip\b|qualified institution|preferential (issue|allotment)|rights issue|fund rais|raising of funds|issue of (warrants|ncd|non-convertible)"),
    ("buyback", "Buyback", 55, "positive", r"buy[- ]?back"),
    ("bonus", "Bonus issue", 45, "positive", r"\bbonus\b"),
    ("split", "Stock split", 35, "positive", r"sub[- ]?division|stock split|split of (equity )?shares"),
    ("dividend", "Dividend", 20, "positive", r"\bdividend\b"),
    ("merger", "Merger / acquisition", 55, "neutral", r"amalgamation|\bmerger\b|acquisition|acquire[sd]?|scheme of arrangement|demerger|takeover|open offer"),
    ("capex", "Expansion / capex", 40, "positive", r"capacity expansion|commission(ed|ing) of|new (plant|facility|unit)|capex|greenfield|brownfield"),
    ("disruption", "Plant disruption", 50, "negative", r"\bfire\b|explosion|shutdown|strike|lock[- ]?out|suspension of operations"),
    ("litigation", "Litigation", 35, "negative", r"litigation|court order|arbitration award|penalty|tax demand|gst demand|demand notice"),
    ("concall", "Concall / investor meet", 25, "neutral", r"transcript|earnings call|conference call|analyst(s)? meet|investor (meet|presentation)|con-?call"),
    ("agm", "AGM / EGM", 20, "neutral", r"annual general meeting|extra-?ordinary general meeting|\bagm\b|\begm\b|postal ballot|voting results"),
    ("kmp_change", "Management change", 30, "neutral", r"appointment of|change in (directors|management|kmp)|cessation"),
    ("clarification", "Clarification", 20, "neutral", r"clarification|price movement|spurt in volume"),
    ("insider", "Insider disclosure", 30, "neutral", r"insider trading|regulation 7|reg(ulation)? 29|\bsast\b|substantial acquisition"),
]
LABEL = {r[0]: r[1] for r in RULES} | {
    "insider_buy": "Insider buy", "insider_sell": "Insider sell", "bulk_buy": "Bulk/block buy", "bulk_sell": "Bulk/block sell",
    "news": "News", "global": "Global", "macro": "Policy / macro", "filing": "Filing", "flows": "FII / DII flows"}
_COMPILED = [(t, b, s, re.compile(p)) for t, _l, b, s, p in RULES]
READ_TYPES = {"results", "order_win", "fund_raise", "merger", "buyback", "capex", "concall", "auditor_exit", "default",
              "insolvency", "sebi_order", "fraud", "rating_downgrade", "rating_upgrade", "litigation", "disruption", "agm", "filing"}


def classify(headline: str, detail: str = "", category: str = "") -> tuple[str, int, str]:
    t = f"{category} | {headline} | {detail}".lower()
    for typ, base, sent, rx in _COMPILED:
        if rx.search(t):
            return typ, base, sent
    return "filing", 15, "neutral"


_CR = re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(crore|cr\b|lakh|lac|million|mn|billion|bn)", re.I)


def money_cr(text: str) -> float | None:
    """Largest money amount mentioned, in crore. Used only for sizing an event, never shown as a fact by itself."""
    best = None
    for m in _CR.finditer(text or ""):
        v = float(m.group(1).replace(",", ""))
        u = m.group(2).lower()
        v = v if u.startswith("cr") else v / 100 if u in ("lakh", "lac") else v / 10 if u in ("million", "mn") else v * 100
        best = v if best is None else max(best, v)
    return best


def summary(typ: str, company: str, headline: str, detail: str = "", numbers: dict | None = None) -> str:
    """Two plain lines from rules (the model's summary replaces this once it has read the document)."""
    who = company or "The company"
    n = numbers or {}
    if typ == "insider_buy" or typ == "insider_sell":
        verb = "bought" if typ == "insider_buy" else "sold"
        return (f"{n.get('person', 'An insider')} ({(n.get('category') or 'insider').lower()}) {verb} "
                f"{n.get('qty_fmt', '')} shares worth about ₹{n.get('value_cr', 0):.2f} crore{(' via ' + n['mode'].lower()) if n.get('mode') else ''}.\n"
                f"Holding {'rose' if typ == 'insider_buy' else 'fell'} from {n.get('pct_before', '?')}% to {n.get('pct_after', '?')}%.")
    if typ in ("bulk_buy", "bulk_sell"):
        return (f"{n.get('party', 'A large investor')} {'bought' if typ == 'bulk_buy' else 'sold'} {n.get('qty_fmt', '')} shares "
                f"at ₹{n.get('price', 0):,.2f} in a {n.get('kind', 'bulk').lower()} deal.\nDeal value about ₹{n.get('value_cr', 0):.2f} crore.")
    if typ == "results_date":
        return f"{who} will hold a board meeting on {n.get('meeting_date', 'a set date')}.\nAgenda: {(n.get('purpose') or headline)[:140]}."
    line1 = (headline or "").strip().rstrip(".")[:180]
    line2 = (detail or "").strip().replace("\n", " ")[:200]
    return f"{who}: {line1}.\n{line2}" if line2 and line2.lower() not in line1.lower() else f"{who}: {line1}."
