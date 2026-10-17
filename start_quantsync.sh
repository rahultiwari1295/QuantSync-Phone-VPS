#!/bin/bash
# QuantSync OnePlus 13s Autonomous Supervisor

echo "[*] Checking QuantSync services..."

# 0. Acquire CPU Wake-Lock & Ensure SSH Daemon
termux-wake-lock 2>/dev/null
if ! pgrep -x sshd >/dev/null 2>&1; then
    echo "[+] Starting SSH daemon on port 8022..."
    sshd
fi

# 1. Start Server tmux session if not running
if ! tmux has-session -t quantsync_server 2>/dev/null; then
    echo "[+] Launching QuantSync Server..."
    tmux new-session -d -s quantsync_server 'proot-distro login ubuntu -- bash -c "cd /root/quantsync && python3 -m uvicorn server:app --host 0.0.0.0 --port 8000"'
else
    echo "[=] QuantSync Server is already running."
fi

# 2. Start Ngrok Static Tunnel if not running
if ! tmux has-session -t quantsync_ngrok 2>/dev/null; then
    echo "[+] Launching Ngrok Static Tunnel (backing-lapping-denial.ngrok-free.dev)..."
    tmux new-session -d -s quantsync_ngrok 'proot-distro login ubuntu -- bash -c "ngrok http 8000 --url=https://backing-lapping-denial.ngrok-free.dev --log stdout"'
else
    echo "[=] Ngrok Tunnel is already running."
fi

# 3. Start Scheduler tmux session if not running
if ! tmux has-session -t quantsync_sched 2>/dev/null; then
    echo "[+] Launching QuantSync Scheduler Daemon..."
    tmux new-session -d -s quantsync_sched 'proot-distro login ubuntu -- bash -c "cd /root/quantsync && python3 scheduler.py --daemon"'
else
    echo "[=] QuantSync Scheduler is already running."
fi

# 4. Ensure Cron Daemon is active inside Ubuntu
proot-distro login ubuntu -- bash -c "pgrep -x cron >/dev/null || service cron start" 2>/dev/null

sleep 2
tmux ls
echo "[✓] QuantSync Autonomous Stack is Active!"
echo "[✓] Public URL: https://backing-lapping-denial.ngrok-free.dev"
