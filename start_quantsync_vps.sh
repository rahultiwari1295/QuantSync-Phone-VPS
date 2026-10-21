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

# 2. Start Ngrok Static Tunnel if not running
if ! tmux has-session -t quantsync_ngrok 2>/dev/null; then
    echo "[+] Launching Ngrok Static Tunnel (backing-lapping-denial.ngrok-free.dev)..."
    tmux new-session -d -s quantsync_ngrok 'ngrok http 8000 --url=https://backing-lapping-denial.ngrok-free.dev --log stdout'
else
    echo "[=] Ngrok Tunnel is already running."
fi

# 3. Start Scheduler tmux session if not running
if ! tmux has-session -t quantsync_sched 2>/dev/null; then
    echo "[+] Launching QuantSync Scheduler Daemon..."
    tmux new-session -d -s quantsync_sched 'cd /root/quantsync && ./venv/bin/python3 scheduler.py --daemon'
else
    echo "[=] QuantSync Scheduler is already running."
fi

# 4. Start Lab Fast Worker if not running
if ! tmux has-session -t quantsync_worker_fast 2>/dev/null; then
    echo "[+] Launching Lab Fast Worker..."
    tmux new-session -d -s quantsync_worker_fast 'cd /opt/quantsync-lab && QSLAB_CONFIG=/opt/quantsync-lab/config.yaml ./venv/bin/python -m lab.worker fast'
else
    echo "[=] Lab Fast Worker is already running."
fi

# 5. Start Lab AI Worker if not running
if ! tmux has-session -t quantsync_worker_ai 2>/dev/null; then
    echo "[+] Launching Lab AI Worker..."
    tmux new-session -d -s quantsync_worker_ai 'cd /opt/quantsync-lab && QSLAB_CONFIG=/opt/quantsync-lab/config.yaml nice -n 10 ./venv/bin/python -m lab.worker ai'
else
    echo "[=] Lab AI Worker is already running."
fi

# 6. Ensure Cron Daemon is active
service cron status >/dev/null 2>&1 || service cron start

# 7. Run DuckDNS update
/root/duckdns/duck.sh >/dev/null 2>&1

sleep 2
tmux ls
echo "[✓] QuantSync Cloud Stack is Active!"
echo "[✓] Public URL: https://backing-lapping-denial.ngrok-free.dev"
