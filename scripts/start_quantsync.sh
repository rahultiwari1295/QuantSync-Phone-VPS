#!/bin/bash
# QuantSync OnePlus 13s AI Model Node Supervisor

echo "[*] Checking QuantSync Phone AI services..."

# 1. Acquire CPU Wake-Lock & Ensure SSH Daemon
termux-wake-lock 2>/dev/null
if ! pgrep -x sshd >/dev/null 2>&1; then
    echo "[+] Starting SSH daemon on port 8022..."
    sshd
fi

# 2. Start Ollama Server in tmux if not running
if ! tmux has-session -t ollama_server 2>/dev/null; then
    echo "[+] Launching Ollama Server on port 11434..."
    tmux new-session -d -s ollama_server 'proot-distro login ubuntu -- bash -c "OLLAMA_HOST=0.0.0.0:11434 ollama serve"'
else
    echo "[=] Ollama Server is already running."
fi

# 3. Start SSH Reverse Tunnel to Cloud VPS (Port 11434 forwarding)
if ! tmux has-session -t quantsync_ollama_tunnel 2>/dev/null; then
    echo "[+] Launching SSH Reverse Tunnel to Cloud VPS (11434 -> localhost:11434)..."
    tmux new-session -d -s quantsync_ollama_tunnel 'while true; do ssh -N -R 11434:localhost:11434 -p 20034 -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=no root@51.68.204.210; sleep 5; done'
else
    echo "[=] SSH Reverse Tunnel is already running."
fi

sleep 2
tmux ls
echo "[✓] QuantSync Phone AI Node is Active (Ollama + Cloud Reverse Tunnel)!"
