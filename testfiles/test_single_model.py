import json
import sys
import time
import requests

model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5:1.5b"
TEXT_FILE = "/root/quantsync/Data/intel_text/6d64eb8efc43b6d9ab0c.txt"

with open(TEXT_FILE, "r") as f:
    filing_text = f.read()

PROMPT_TEMPLATE = """You read Indian stock-exchange filings for retail investors. Answer ONLY with JSON, no prose.
Schema:
{
  "summary": "2-3 short sentences. What actually happened, plain English, no corporate jargon. If guidance was given, quote the numbers.",
  "what_changed": "1 bullet point. The single operational or balance-sheet change. Empty string if just routine compliance.",
  "sentiment": "positive" | "negative" | "neutral",
  "key_numbers": [{"label": "revenue | pat | margin | order_size | capex | stake_pct | other", "value": "quoted string from doc"}],
  "perspective": {
    "why_now": "1 sentence on why the company filed this today",
    "who_benefits": "shareholders | promoters | lenders | customers | neutral",
    "watch_next": "1 concrete trigger or date"
  },
  "red_flags": ["any audit qualification, default, resignation, SEBI action, pledge increase"],
  "confidence": 0.0 to 1.0
}
Rules:
- Never guess numbers. If a key_number isn't in the text, do NOT invent it.
- Routine items (board meeting notice, newspaper ad, loss of share cert): keep summary very brief, sentiment neutral.
- Mergers, defaults, raids, resignation of auditor: sentiment negative unless unambiguously saving the company.
- Material orders, capacity additions, turnaround: sentiment positive.

DOCUMENT TEXT (Symbol: AFFLE):
"""

prompt = PROMPT_TEMPLATE + filing_text

print(f"Testing model: {model} ...")
t0 = time.time()
r = requests.post("http://127.0.0.1:11434/api/generate", json={"model": model, "prompt": prompt, "stream": False, "options": {"temperature": 0.1}}, timeout=180)
t1 = time.time()
data = r.json()

p_tokens = data.get("prompt_eval_count", 0)
p_dur = (data.get("prompt_eval_duration") or 0) / 1e9
g_tokens = data.get("eval_count", 0)
g_dur = (data.get("eval_duration") or 0) / 1e9

print(f"RESULT FOR {model}:")
print(f"Total time: {t1-t0:.2f}s")
if p_dur > 0:
    print(f"Prompt processing: {p_tokens} tokens in {p_dur:.2f}s -> {p_tokens/p_dur:.1f} tok/s")
if g_dur > 0:
    print(f"Token generation: {g_tokens} tokens in {g_dur:.2f}s -> {g_tokens/g_dur:.1f} tok/s")

resp = data.get("response", "").strip()
print(f"Raw Response:\n{resp}")
