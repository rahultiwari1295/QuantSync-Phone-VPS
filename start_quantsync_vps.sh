#!/bin/bash
# QuantSync Cloud VPS Native Supervisor
echo "[*] Checking QuantSync Cloud VPS services..."

# 1. Start Server tmux session if not running
if ! tmux has-session -t quantsync_server 2>/dev/null; then
    echo "[+] Launching QuantSync FastAPI Server on port 8000..."
    tmux new-session -d -s quantsync_server 'cd /root/quantsync && ./venv/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8000'
else
    echo "[=] QuantSync Server is already running."
fi

# 2. Start Scheduler tmux session if not running
if ! tmux has-session -t quantsync_sched 2>/dev/null; then
    echo "[+] Launching QuantSync Scheduler Daemon..."
    tmux new-session -d -s quantsync_sched 'cd /root/quantsync && ./venv/bin/python3 scheduler.py --daemon'
else
    echo "[=] QuantSync Scheduler is already running."
fi

# 3. Ensure Cron Daemon is active
service cron status >/dev/null 2>&1 || service cron start

# 4. Run DuckDNS update
/root/duckdns/duck.sh >/dev/null 2>&1

sleep 2
tmux ls
echo "[✓] QuantSync Cloud Stack is Active!"
