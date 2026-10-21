import subprocess
import time
import json
import urllib.request
import os
import signal

MODELS = {
    "1. Qwen 2.5 0.5B Stock-News FineTune (HF)": {
        "path": "/root/quantsync/models/qwen-0.5b-stocks-news-v2-q4_k_m.gguf",
        "disk_size_mb": 380,
        "format": "Q4_K_M GGUF (Fine-tuned on Stock News)",
        "chat_wrap": lambda sys, usr: f"<|im_start|>system\n{sys}<|im_end|>\n<|im_start|>user\n{usr}<|im_end|>\n<|im_start|>assistant\n",
        "stop": ["<|im_end|>"]
    },
    "2. Qwen 2.5 (0.5B Instruct)": {
        "path": "/usr/share/ollama/.ollama/models/blobs/sha256-c5396e06af294bd101b30dce59131a76d2b773e76950acc870eda801d3ab0515",
        "disk_size_mb": 397,
        "format": "Q4_K_M GGUF (General Instruct)",
        "chat_wrap": lambda sys, usr: f"<|im_start|>system\n{sys}<|im_end|>\n<|im_start|>user\n{usr}<|im_end|>\n<|im_start|>assistant\n",
        "stop": ["<|im_end|>"]
    },
    "3. DeepSeek-R1 (1.5B Distill Reasoning)": {
        "path": "/usr/share/ollama/.ollama/models/blobs/sha256-aabd4debf0c8f08881923f2c25fc0fdeed24435271c2b3e92c4af36704040dbc",
        "disk_size_mb": 1117,
        "format": "Q4_K_M GGUF (DeepSeek Reasoning CoT)",
        "chat_wrap": lambda sys, usr: f"<|im_start|>system\n{sys}<|im_end|>\n<|im_start|>user\n{usr}<|im_end|>\n<|im_start|>assistant\n",
        "stop": ["<|im_end|>"]
    }
}

TESTS = [
    {
        "id": "trade_thesis",
        "name": "Task 1: Pattern Trade Thesis",
        "sys": "You are a quantitative swing trader. Give concise, institutional rationale.",
        "usr": "Stock: NURECA (CMP: 364.80). Setup: 24-day consolidation breakout on 4.2x volume, RSI 62. Provide a 2-sentence actionable trade thesis.",
        "n_predict": 75,
        "temp": 0.1
    },
    {
        "id": "risk_math",
        "name": "Task 2: Financial Math & Risk Management",
        "sys": "You are a risk management engine. Provide exact numbers.",
        "usr": "Stock entry is 364.80. Stop loss is 351.00. Calculate risk amount per share and target price for 1:2 Risk-Reward ratio. State the numbers clearly.",
        "n_predict": 75,
        "temp": 0.1
    },
    {
        "id": "json_structured",
        "name": "Task 3: Structured JSON Output",
        "sys": "You are an automated API returning ONLY valid JSON. No preamble, no explanation.",
        "usr": "Convert this alert into JSON with keys ticker, action, entry, stop_loss, confidence: 'BUY TRENT at 7150 with stop 6950, very high conviction'",
        "n_predict": 75,
        "temp": 0.1
    }
]

def get_proc_rss_mb(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0

results = {}

for m_name, m_cfg in MODELS.items():
    print(f"\n==========================================")
    print(f"BENCHMARKING: {m_name}")
    print(f"==========================================")
    
    t_start_boot = time.time()
    cmd = [
        "/usr/local/lib/ollama/llama-server",
        "-m", m_cfg["path"],
        "--port", "8089",
        "-t", "2",
        "-tb", "2",
        "-c", "1024",
        "-np", "1",
        "-fa", "off",
        "--no-warmup"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    ready = False
    for _ in range(40):
        time.sleep(0.3)
        try:
            req = urllib.request.urlopen("http://127.0.0.1:8089/health", timeout=1)
            if req.status == 200:
                ready = True
                break
        except Exception:
            pass
            
    if not ready:
        print(f"Error: failed to boot {m_name}")
        os.kill(proc.pid, signal.SIGKILL)
        continue

    boot_time = time.time() - t_start_boot
    ram_idle = get_proc_rss_mb(proc.pid)
    print(f"Boot Time: {boot_time:.2f}s | Idle RAM: {ram_idle:.1f} MB")
    
    m_results = {
        "disk_size_mb": m_cfg["disk_size_mb"],
        "format": m_cfg["format"],
        "boot_time_sec": round(boot_time, 2),
        "ram_idle_mb": round(ram_idle, 1),
        "tests": []
    }
    
    max_ram_seen = ram_idle
    for test in TESTS:
        prompt = m_cfg["chat_wrap"](test["sys"], test["usr"])
        data = json.dumps({
            "prompt": prompt,
            "n_predict": test["n_predict"],
            "temperature": test["temp"],
            "stop": m_cfg["stop"]
        }).encode("utf-8")
        
        t0 = time.time()
        req = urllib.request.Request("http://127.0.0.1:8089/completion", data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=60)
        dt = time.time() - t0
        res = json.loads(resp.read().decode("utf-8"))
        
        cur_ram = get_proc_rss_mb(proc.pid)
        if cur_ram > max_ram_seen:
            max_ram_seen = cur_ram
            
        timings = res.get("timings", {})
        prompt_tps = timings.get("prompt_per_second", 0)
        gen_tps = timings.get("predicted_per_second", 0)
        gen_tokens = timings.get("predicted_n", 0)
        prompt_tokens = timings.get("prompt_n", 0)
        content = res.get("content", "").strip()
        
        print(f"\n--- {test['name']} ---")
        print(f"Latency: {dt:.2f}s | Gen Speed: {gen_tps:.2f} tok/s | Tokens: {gen_tokens}")
        print(f"Output:\n{content}")
        
        m_results["tests"].append({
            "id": test["id"],
            "name": test["name"],
            "latency_sec": round(dt, 2),
            "prompt_tokens": prompt_tokens,
            "prompt_tps": round(prompt_tps, 2),
            "gen_tokens": gen_tokens,
            "gen_tps": round(gen_tps, 2),
            "output": content
        })
        
    m_results["ram_peak_mb"] = round(max_ram_seen, 1)
    results[m_name] = m_results
    
    # Teardown
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    time.sleep(1)

with open("/root/quantsync/benchmark_results_custom.json", "w") as f:
    json.dump(results, f, indent=2)

print("\n\nCustom benchmarks completed! Results written to /root/quantsync/benchmark_results_custom.json")
