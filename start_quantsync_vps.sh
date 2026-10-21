#!/bin/bash
# QuantSync Cloud VPS Native Supervisor
echo "[*] Checking QuantSync Cloud VPS systemd services..."

systemctl is-active --quiet quantsync-api.service || systemctl start quantsync-api.service
systemctl is-active --quiet quantsync-scheduler.service || systemctl start quantsync-scheduler.service

# Start Lab Fast Worker if not running
if ! tmux has-session -t quantsync_worker_fast 2>/dev/null; then
    echo "[+] Launching Lab Fast Worker..."
    tmux new-session -d -s quantsync_worker_fast 'cd /opt/quantsync-lab && QSLAB_CONFIG=/opt/quantsync-lab/config.yaml ./venv/bin/python -m lab.worker fast'
fi

# Start Lab AI Worker if not running
if ! tmux has-session -t quantsync_worker_ai 2>/dev/null; then
    echo "[+] Launching Lab AI Worker..."
    tmux new-session -d -s quantsync_worker_ai 'cd /opt/quantsync-lab && QSLAB_CONFIG=/opt/quantsync-lab/config.yaml nice -n 10 ./venv/bin/python -m lab.worker ai'
fi

# Ensure Cron Daemon is active
service cron status >/dev/null 2>&1 || service cron start

# Run DuckDNS update
/root/duckdns/duck.sh >/dev/null 2>&1

echo "[✓] QuantSync Cloud Stack is Active!"
echo "[✓] Public URL: https://quantsync-api.duckdns.org:40034"
