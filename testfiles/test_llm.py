import urllib.request
import json
import time

payload = json.dumps({
    "model": "qwen2.5:0.5b",
    "prompt": "Explain algorithmic trading in 3 bullet points.",
    "stream": False,
    "options": {"num_predict": 64}
}).encode('utf-8')

req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=payload, headers={"Content-Type": "application/json"})
t0 = time.time()
with urllib.request.urlopen(req, timeout=90) as r:
    data = json.loads(r.read().decode('utf-8'))

total_time = time.time() - t0
eval_count = data.get('eval_count', 0)
eval_dur = data.get('eval_duration', 1) / 1e9
p_count = data.get('prompt_eval_count', 0)
p_dur = data.get('prompt_eval_duration', 1) / 1e9

gen_speed = eval_count / eval_dur if eval_dur > 0 else 0
prompt_speed = p_count / p_dur if p_dur > 0 else 0

print(f"Prompt Tokens: {p_count} in {p_dur:.3f}s -> {prompt_speed:.2f} tok/s")
print(f"Generated Tokens: {eval_count} in {eval_dur:.3f}s -> {gen_speed:.2f} tok/s")
print(f"Total Wall Time: {total_time:.3f}s")
