#!/usr/bin/env bash
cd /root/quantsync
/root/quantsync/venv/bin/python3 /root/quantsync/trendlyne_collector.py --fetch --db /root/quantsync/Data/market_intel.db >> /root/quantsync/Data/logs/trendlyne_sync.log 2>&1
