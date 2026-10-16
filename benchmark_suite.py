import time
import os
import sys
import json
import sqlite3
import numpy as np
import pandas as pd
import xgboost as xgb

def print_header(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")

results = {}

# 1. CPU & Matrix Multiplication (NumPy BLAS)
def bench_matrix_multiplication():
    print("[1/5] Running NumPy Matrix Multiplication (2048 x 2048 float64)...")
    np.random.seed(42)
    A = np.random.randn(2048, 2048).astype(np.float64)
    B = np.random.randn(2048, 2048).astype(np.float64)
    
    # Warmup
    _ = np.dot(A[:256, :256], B[:256, :256])
    
    t0 = time.perf_counter()
    C = np.dot(A, B)
    elapsed = time.perf_counter() - t0
    
    # Flops for N x N matrix multiply: 2 * N^3
    gflops = (2 * (2048 ** 3)) / (elapsed * 1e9)
    print(f"      Time: {elapsed:.3f} s | Computed: {gflops:.2f} GFLOPS")
    results['matrix_multiply_seconds'] = round(elapsed, 4)
    results['matrix_multiply_gflops'] = round(gflops, 2)

# 2. Heavy Quant Indicator Crunch (500,000 candles)
def bench_quant_indicators():
    print("[2/5] Running Quant Trading Engine Crunch (500,000 candles: RSI, EMA, Bollinger, VWAP)...")
    n = 500_000
    np.random.seed(42)
    returns = np.random.normal(0.0002, 0.01, n)
    price = 1000.0 * np.exp(np.cumsum(returns))
    high = price * (1.0 + np.abs(np.random.normal(0, 0.003, n)))
    low = price * (1.0 - np.abs(np.random.normal(0, 0.003, n)))
    volume = np.random.randint(100, 10000, n).astype(np.float64)
    
    df = pd.DataFrame({'close': price, 'high': high, 'low': low, 'volume': volume})
    
    t0 = time.perf_counter()
    # EMA 9, 21, 50, 200
    ema9 = df['close'].ewm(span=9, adjust=False).mean()
    ema21 = df['close'].ewm(span=21, adjust=False).mean()
    ema50 = df['close'].ewm(span=50, adjust=False).mean()
    ema200 = df['close'].ewm(span=200, adjust=False).mean()
    
    # RSI 14
    delta = df['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    
    # Bollinger Bands 20
    sma20 = df['close'].rolling(window=20).mean()
    std20 = df['close'].rolling(window=20).std()
    upper_bb = sma20 + 2 * std20
    lower_bb = sma20 - 2 * std20
    
    # VWAP
    cum_pv = (df['close'] * df['volume']).cumsum()
    cum_vol = df['volume'].cumsum()
    vwap = cum_pv / (cum_vol + 1e-9)
    
    elapsed = time.perf_counter() - t0
    candles_per_sec = n / elapsed
    print(f"      Time: {elapsed:.3f} s | Throughput: {candles_per_sec:,.0f} candles/sec")
    results['quant_indicators_seconds'] = round(elapsed, 4)
    results['quant_candles_per_sec'] = round(candles_per_sec, 0)

# 3. Database Storage & I/O (50,000 SQLite inserts + range query)
def bench_sqlite_io():
    print("[3/5] Running SQLite WAL Batch Storage Test (50,000 tick records)...")
    db_file = "/tmp/bench_test.db"
    if os.path.exists(db_file):
        os.remove(db_file)
        
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("CREATE TABLE ticks (id INTEGER PRIMARY KEY, symbol TEXT, price REAL, volume INTEGER, ts REAL)")
    
    rows = [(i, f"SYM_{i%100}", 100.0 + (i % 50), 1000 + (i % 200), 1700000000.0 + i) for i in range(50000)]
    
    t0 = time.perf_counter()
    conn.executemany("INSERT INTO ticks VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    write_time = time.perf_counter() - t0
    
    # Index and query
    t1 = time.perf_counter()
    conn.execute("CREATE INDEX idx_sym_price ON ticks (symbol, price)")
    cur = conn.cursor()
    cur.execute("SELECT symbol, AVG(price), SUM(volume) FROM ticks WHERE price > 120 GROUP BY symbol")
    _ = cur.fetchall()
    read_time = time.perf_counter() - t1
    conn.close()
    
    if os.path.exists(db_file):
        os.remove(db_file)
    if os.path.exists(db_file + "-wal"):
        os.remove(db_file + "-wal")
        
    total_time = write_time + read_time
    print(f"      Write 50k rows: {write_time:.3f} s | Index + Query: {read_time:.3f} s | Total: {total_time:.3f} s")
    results['sqlite_write_seconds'] = round(write_time, 4)
    results['sqlite_query_seconds'] = round(read_time, 4)

# 4. Machine Learning: XGBoost Training & Inference
def bench_xgboost():
    print("[4/5] Running XGBoost Gradient Boosted Trees (50,000 samples, 20 features, 100 trees)...")
    np.random.seed(42)
    X = np.random.randn(50000, 20).astype(np.float32)
    y = np.random.randn(50000).astype(np.float32)
    
    dtrain = xgb.DMatrix(X, label=y)
    params = {
        'max_depth': 6,
        'eta': 0.1,
        'objective': 'reg:squarederror',
        'tree_method': 'hist',
        'nthread': -1
    }
    
    # Training
    t0 = time.perf_counter()
    bst = xgb.train(params, dtrain, num_boost_round=100)
    train_time = time.perf_counter() - t0
    
    # Inference on 50k samples
    t1 = time.perf_counter()
    preds = bst.predict(dtrain)
    inf_time = time.perf_counter() - t1
    inf_throughput = 50000 / inf_time
    
    print(f"      Train Time: {train_time:.3f} s | Inference Time: {inf_time:.3f} s ({inf_throughput:,.0f} samples/sec)")
    results['xgboost_train_seconds'] = round(train_time, 4)
    results['xgboost_inf_seconds'] = round(inf_time, 4)
    results['xgboost_inf_samples_per_sec'] = round(inf_throughput, 0)

# 5. LLM Inference Speed Test
def bench_llm():
    print("[5/5] Running LLM Generation Benchmark via Ollama...")
    import urllib.request
    
    prompt = "Explain momentum trading and institutional order blocks in the Indian stock market in 4 concise bullet points."
    model = "qwen2.5:0.5b"
    
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.2,
            "num_predict": 128
        }
    }).encode('utf-8')
    
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate", data=payload, headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        total_time = time.perf_counter() - t0
        
        eval_count = data.get('eval_count', 0)
        eval_duration = data.get('eval_duration', 1) / 1e9  # ns to s
        prompt_eval_count = data.get('prompt_eval_count', 0)
        prompt_eval_duration = data.get('prompt_eval_duration', 1) / 1e9
        
        tok_per_sec = eval_count / eval_duration if eval_duration > 0 else 0
        prompt_tok_per_sec = prompt_eval_count / prompt_eval_duration if prompt_eval_duration > 0 else 0
        
        print(f"      Model: {model}")
        print(f"      Prompt Tokens: {prompt_eval_count} ({prompt_tok_per_sec:.1f} tok/s)")
        print(f"      Generated Tokens: {eval_count} in {eval_duration:.2f} s ({tok_per_sec:.2f} tok/s)")
        print(f"      Total Time: {total_time:.2f} s")
        results['llm_model'] = model
        results['llm_gen_tokens_per_sec'] = round(tok_per_sec, 2)
        results['llm_prompt_tokens_per_sec'] = round(prompt_tok_per_sec, 2)
        results['llm_total_seconds'] = round(total_time, 2)
    except Exception as e:
        print(f"      LLM Test skipped or unavailable: {e}")
        results['llm_status'] = f"Unavailable: {e}"

if __name__ == '__main__':
    print_header("QUANTSYNC 1-MINUTE PRODUCTION BENCHMARK")
    total_start = time.perf_counter()
    bench_matrix_multiplication()
    bench_quant_indicators()
    bench_sqlite_io()
    bench_xgboost()
    bench_llm()
    total_elapsed = time.perf_counter() - total_start
    print_header("BENCHMARK COMPLETED")
    print(f"Total Suite Runtime: {total_elapsed:.2f} seconds")
    results['total_suite_runtime'] = round(total_elapsed, 2)
    print("\nRESULTS_JSON:" + json.dumps(results))
