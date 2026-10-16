#!/bin/bash
cd /root/quantsync
LOG_FILE="/root/quantsync/logs/full_universe_reingestion.log"
PID_FILE="/root/quantsync/logs/pipeline.pid"
echo $$ > "$PID_FILE"

echo "=================================================================" >> "$LOG_FILE"
echo "=== [$(date)] STARTING DUAL-BROKER FULL RE-INGESTION PIPELINE ===" >> "$LOG_FILE"
echo "=================================================================" >> "$LOG_FILE"

# Step 1: Re-ingest daily bars across ~2,700 stocks with both brokers in parallel
./venv/bin/python3 scripts/run_full_universe_dual_ingest.py

# Step 2: Compute daily technicals (CMP, EMA20/50/200, RSI, MACD, Minervini)
echo "=== [$(date)] COMPUTING DAILY TECHNICALS FOR ALL STOCKS ===" >> "$LOG_FILE"
./venv/bin/python3 scripts/compute_daily_technicals.py >> "$LOG_FILE" 2>&1

# Step 3: Rebuild screener snapshot with today's fresh CMP
echo "=== [$(date)] REBUILDING SCREENER SNAPSHOT TABLE ===" >> "$LOG_FILE"
./venv/bin/python3 build_screener_snapshot.py --date $(date +%Y-%m-%d) >> "$LOG_FILE" 2>&1

echo "=================================================================" >> "$LOG_FILE"
echo "=== [$(date)] PIPELINE COMPLETED SUCCESSFULLY! ===" >> "$LOG_FILE"
echo "=================================================================" >> "$LOG_FILE"
rm -f "$PID_FILE"
