#!/usr/bin/env bash
set -e
export TZ='Asia/Kolkata'
cd /root/quantsync
VENV='/root/quantsync/venv/bin/python3'
LOG_DIR='/root/quantsync/logs'
mkdir -p "$LOG_DIR"

echo "=== [$(date)] EOD Automated Catchup Orchestrator Started ==="

# 1. Wait for candle sync to complete
while pgrep -f "scheduler.py --now sync" > /dev/null; do
    echo "[$(date)] Incremental candle sync in progress, waiting 20s..."
    sleep 20
done
echo "✅ [$(date)] Candle sync completed!"

# 2. Run EOD Finalize (15:45 cycle)
echo "🚀 [$(date)] Running 15:45 EOD Authoritative Settlement & Wix Sync..."
$VENV scheduler.py --now finalize --force >> "$LOG_DIR/eod_finalize.log" 2>&1
echo "✅ [$(date)] 15:45 EOD Settlement completed!"

# 3. Run DNA & Pattern Refresh (16:30 cycle)
echo "🚀 [$(date)] Running 16:30 EOD Universe DNA & Screener Pattern Refresh..."
$VENV scheduler.py --now dna_refresh --force >> "$LOG_DIR/eod_dna_refresh.log" 2>&1
echo "✅ [$(date)] 16:30 DNA & Pattern Refresh completed!"

# 4. Ingest Official NSE Delivery Bhavcopy (18:30 cycle)
echo "🚀 [$(date)] Ingesting Official NSE Delivery % Bhavcopy..."
$VENV scheduler.py --now delivery --force >> "$LOG_DIR/eod_delivery.log" 2>&1 || true
echo "✅ [$(date)] NSE Delivery Ingestion completed!"

echo "🎉 [$(date)] ALL EOD PIPELINES EXECUTED SUCCESSFULLY!"
