"""Filing reader: PDF -> text -> local model (Ollama) -> fixed JSON, stored on the event.

Design rules
  * Rules and structured sources supply every exact number; the model only explains. Any number the model quotes
    that does not appear in the document text is dropped before saving.
  * Short documents are read whole. Long ones are cut into chunks; each chunk is summarised, then the summaries are
    merged. Documents longer than MAX_CHUNKS chunks are only read for their first and most relevant parts.
  * The phone model (QS_LLM_MODEL, default gemma3n:e4b) handles the live queue. Set QS_OLLAMA_HEAVY to a bigger
    machine (your PC) and long documents go there instead.
  * Highest impact first; a time budget per run keeps the phone responsive."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import requests

from . import db
from .net import Net

OLLAMA = os.environ.get("QS_OLLAMA", "http://127.0.0.1:11434").rstrip("/")
HEAVY = os.environ.get("QS_OLLAMA_HEAVY", "").rstrip("/")
MODEL = os.environ.get("QS_LLM_MODEL", "qwen2.5:1.5b")
HEAVY_MODEL = os.environ.get("QS_LLM_HEAVY_MODEL", MODEL)
CHUNK = 9000          # characters per chunk (~2.3K tokens)
MAX_CHUNKS = 6
TEXT_DIR = os.path.join(db.DATA_DIR, "intel_text")

PROMPT = """You read Indian stock-exchange filings for retail investors. Answer ONLY with JSON, no prose.
Company: {company} ({symbol}). Filing: {headline}

Return exactly:
{{"summary": "two short plain-English sentences: what happened and why it matters",
  "what_changed": "one sentence: what is new versus before (or 'Nothing new')",
  "sentiment": "positive|negative|neutral",
  "key_numbers": [{{"label": "...", "value": "copy the number exactly as written in the text"}}],
  "perspective": {{"why_now": "...", "who_benefits": "promoters|minority shareholders|lenders|employees|unclear", "watch_next": "..."}},
  "red_flags": ["..."],
  "confidence": 0.0}}
Rules: never invent numbers; copy them exactly from the text. Max 5 key_numbers. Keep every string under 220 characters.
If the text is only a cover letter with no substance, say so in summary and set confidence below 0.3.

TEXT:
{text}"""

MERGE = """Combine these partial notes about ONE filing into the same JSON format. Keep only numbers that appear in the notes.
Company: {company} ({symbol}). Filing: {headline}
NOTES:
{notes}"""


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
        r = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in r.pages[:120])
    except Exception:  # noqa: BLE001 - fall back to poppler
        pass
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(data); f.flush()
            out = subprocess.run(["pdftotext", "-layout", "-l", "120", f.name, "-"], capture_output=True, timeout=120)
            return out.stdout.decode("utf-8", "replace")
    return ""


def clean(t: str) -> str:
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def ask(prompt: str, heavy: bool = False, timeout: int = 900) -> dict:
    base, model = (HEAVY, HEAVY_MODEL) if heavy and HEAVY else (OLLAMA, MODEL)
    r = requests.post(f"{base}/api/generate", json={"model": model, "prompt": prompt, "format": "json", "stream": False,
                                                    "options": {"temperature": 0.1, "num_ctx": 8192}}, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json().get("response") or "{}")


def available() -> bool:
    try:
        return requests.get(f"{OLLAMA}/api/tags", timeout=4).ok
    except requests.RequestException:
        return False


def _verify(out: dict, text: str) -> dict:
    flat = re.sub(r"[\s,]", "", text)
    keep = []
    for k in (out.get("key_numbers") or [])[:5]:
        v = str((k or {}).get("value", ""))
        digits = re.sub(r"[^\d.]", "", v)
        if digits and digits.strip(".") and digits in flat:
            keep.append({"label": str(k.get("label", ""))[:80], "value": v[:40]})
    out["key_numbers"] = keep
    if out.get("sentiment") not in ("positive", "negative", "neutral"):
        out["sentiment"] = "neutral"
    try:
        out["confidence"] = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
    except (TypeError, ValueError):
        out["confidence"] = 0.5
    for k in ("summary", "what_changed"):
        out[k] = str(out.get(k, ""))[:400]
    return out


def read_one(ev: dict, net: Net) -> dict:
    data = net.bytes(ev["url"], "nsearch" if "nseindia" in (ev["url"] or "") else "web")
    text = clean(pdf_text(data))
    if len(text) < 200:
        return {"status": "skipped", "reason": "no readable text (scanned image or cover letter)"}
    os.makedirs(TEXT_DIR, exist_ok=True)
    with open(os.path.join(TEXT_DIR, f"{ev['id']}.txt"), "w", encoding="utf-8") as f:
        f.write(text[:400000])
    ctx = {"company": ev.get("company") or ev.get("symbol") or "", "symbol": ev.get("symbol") or "", "headline": ev["headline"]}
    chunks = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    heavy = len(chunks) > 2
    if len(chunks) > MAX_CHUNKS:  # first pages + the chunks that mention the most money / results words
        score = lambda c: len(re.findall(r"crore|lakh|revenue|profit|ebitda|order|guidance|capex|margin|auditor|qualified", c, re.I))
        rest = sorted(range(1, len(chunks)), key=lambda i: -score(chunks[i]))[:MAX_CHUNKS - 1]
        chunks = [chunks[0]] + [chunks[i] for i in sorted(rest)]
    if len(chunks) == 1:
        out = ask(PROMPT.format(text=chunks[0], **ctx), heavy=False)
    else:
        notes = [ask(PROMPT.format(text=c, **ctx), heavy=heavy) for c in chunks]
        out = ask(MERGE.format(notes=json.dumps(notes, ensure_ascii=False)[:24000], **ctx), heavy=heavy)
    out = _verify(out, text)
    out.update(model=HEAVY_MODEL if heavy and HEAVY else MODEL, chars=len(text), chunks=len(chunks), read_at=time.time())
    return {"status": "done", "result": out}


def run(budget_s: int = 1500, limit: int = 40, log=print) -> dict:
    """Read pending filings, highest impact first, until the time budget runs out."""
    if not available():
        log(f"ollama not reachable at {OLLAMA}; skipping")
        return {"read": 0, "skipped": "no model"}
    t0, net, done, failed = time.time(), Net(timeout=40), 0, 0
    with db.session() as con:
        rows = con.execute("SELECT * FROM events WHERE ai_status='pending' AND known_at >= date('now','-10 day') "
                           "ORDER BY impact DESC, ts DESC LIMIT ?", (limit,)).fetchall()
        for r in rows:
            if time.time() - t0 > budget_s:
                break
            ev = dict(r)
            try:
                res = read_one(ev, net)
                if res["status"] == "done":
                    o = res["result"]
                    con.execute("UPDATE events SET ai_status='done', ai_json=?, summary=?, sentiment=? WHERE id=?",
                                (db.jdump(o), (o.get("summary") or ev["summary"])[:600], o.get("sentiment") or ev["sentiment"], ev["id"]))
                    done += 1
                else:
                    con.execute("UPDATE events SET ai_status='skipped', ai_json=? WHERE id=?", (db.jdump(res), ev["id"]))
            except Exception as e:  # noqa: BLE001
                failed += 1
                con.execute("UPDATE events SET ai_status='failed', ai_json=? WHERE id=?", (db.jdump({"error": str(e)[:300]}), ev["id"]))
            con.commit()
            log(f"read {ev['symbol']} {ev['type']}: done={done} failed={failed}")
    return {"read": done, "failed": failed, "seconds": round(time.time() - t0)}
