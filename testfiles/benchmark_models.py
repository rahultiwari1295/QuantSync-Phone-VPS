import json
import time
import requests

MODELS = ["qwen2.5:1.5b", "qwen2.5:3b", "gemma4:e2b"]
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

results = []
print("Starting benchmark across 3 models on phone...")

for m in MODELS:
    print(f"\n==========================================")
    print(f"Testing model: {m}")
    payload = {
        "model": m,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1
        }
    }
    t0 = time.time()
    try:
        r = requests.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=300)
        t1 = time.time()
        data = r.json()
        
        total_time_s = round(t1 - t0, 2)
        load_s = round((data.get("load_duration") or 0) / 1e9, 2)
        prompt_tokens = data.get("prompt_eval_count", 0)
        prompt_eval_s = round((data.get("prompt_eval_duration") or 0) / 1e9, 2)
        prompt_tps = round(prompt_tokens / prompt_eval_s, 1) if prompt_eval_s > 0 else 0
        
        gen_tokens = data.get("eval_count", 0)
        gen_eval_s = round((data.get("eval_duration") or 0) / 1e9, 2)
        gen_tps = round(gen_tokens / gen_eval_s, 1) if gen_eval_s > 0 else 0
        
        resp_text = data.get("response", "").strip()
        
        json_valid = False
        parsed = {}
        clean_json_str = resp_text
        if "```json" in clean_json_str:
            clean_json_str = clean_json_str.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json_str:
            clean_json_str = clean_json_str.split("```")[1].split("```")[0].strip()
            
        try:
            parsed = json.loads(clean_json_str)
            json_valid = True
        except Exception as e:
            json_valid = False
            
        results.append({
            "model": m,
            "total_time_s": total_time_s,
            "load_time_s": load_s,
            "prompt_tokens": prompt_tokens,
            "prompt_tps": prompt_tps,
            "gen_tokens": gen_tokens,
            "gen_tps": gen_tps,
            "json_valid": json_valid,
            "summary": parsed.get("summary", ""),
            "what_changed": parsed.get("what_changed", ""),
            "sentiment": parsed.get("sentiment", ""),
            "key_numbers": parsed.get("key_numbers", []),
            "perspective": parsed.get("perspective", {}),
            "red_flags": parsed.get("red_flags", [])
        })
        print(f"-> Finished in {total_time_s}s | Prompt: {prompt_tps} tok/s | Gen: {gen_tps} tok/s | JSON valid: {json_valid}")
    except Exception as e:
        print(f"Error testing {m}: {e}")

print("\n\n################ BENCHMARK SUMMARY ################")
print(json.dumps(results, indent=2))
