# QuantSync Master VPS Connection Guide

This document is the **single authoritative reference** for connecting to the two active QuantSync nodes:
1. **Cloud Production VPS** (Ubuntu 24.04 LTS on Intel Xeon)
2. **Phone Local VPS** (OnePlus 13s on Snapdragon 8 Elite)

All other historical VPS connections (Alibaba, old IPs) are decommissioned and removed.

---

## 1. Quick Connection Summary Table

| Parameter | 1. Cloud Production VPS (Primary) | 2. Phone Local VPS (Secondary) |
| :--- | :--- | :--- |
| **Node Type** | 24/7 Cloud Production Server | Local Edge / AI Model Node |
| **Host / IP** | `51.68.204.210` | `192.168.29.163` (Home Wi-Fi) |
| **SSH Port** | `20034` | `8022` |
| **Username** | `root` | `u0_a565` |
| **Password** | `YE2QvrGLPiNqvVJt` | *(Passwordless SSH Key)* |
| **OS Distro** | Ubuntu 24.04 LTS (`noble`) | Ubuntu 26.04 (`proot` on Termux) |
| **CPU Arch** | Intel Xeon E3-1270 v6 @ 3.80GHz (x86_64) | Snapdragon 8 Elite (aarch64) |
| **RAM** | 2.0 GB RAM | 12.0 GB LPDDR5X |
| **Storage** | 20 GB SSD | UFS 4.0 Flash Storage |
| **Mac Alias** | `ssh quantsync-cloud` | `ssh quantsync-phone` |

---

## 2. Connecting to the Cloud Production VPS

### Method A: One-Click Alias (Configured in `~/.ssh/config`)
Your Mac's SSH key (`~/.ssh/id_ed25519`) has already been authorized on this server. You can connect instantly from your Mac terminal without entering a password:
```bash
ssh quantsync-cloud
```

### Method B: Direct Command with Port & Password
From any computer or mobile terminal:
```bash
ssh root@51.68.204.210 -p 20034
```
* **Password**: `YE2QvrGLPiNqvVJt`

### Method C: One-Liner with Auto-Password (`sshpass`)
If running non-interactive scripts from your Mac:
```bash
sshpass -p 'YE2QvrGLPiNqvVJt' ssh -p 20034 root@51.68.204.210
```

---

## 3. Connecting to the Phone Local VPS (OnePlus 13s)

### Method A: One-Click Alias
```bash
ssh quantsync-phone
```

### Method B: Direct Wi-Fi Command
```bash
ssh -p 8022 u0_a565@192.168.29.163
```

### Method C: Direct Jump into Ubuntu PRoot
```bash
ssh -p 8022 u0_a565@192.168.29.163 "proot-distro login ubuntu"
```

> [!NOTE]
> If the phone connection ever says `Connection refused`, simply open the **Termux** app on the phone and run:
> ```bash
> termux-wake-lock && sshd
> ```

---

## 4. File Transfers (SCP & RSYNC)

### To Cloud VPS (`51.68.204.210`):
```bash
# Upload a file:
scp -P 20034 myfile.txt root@51.68.204.210:/root/

# Download a file:
scp -P 20034 root@51.68.204.210:/root/quantsync/Data/stock_history.db ./

# Rsync a whole folder:
rsync -avz -e 'ssh -p 20034' ./myfolder/ root@51.68.204.210:/root/myfolder/
```

### To Phone VPS (`192.168.29.163`):
```bash
# Upload a file:
scp -P 8022 myfile.txt u0_a565@192.168.29.163:/data/data/com.termux/files/home/

# Rsync to Phone Ubuntu container:
rsync -avz -e 'ssh -p 8022' ./myfolder/ u0_a565@192.168.29.163:/data/data/com.termux/files/home/
```

---

## 5. Mac SSH Configuration (`~/.ssh/config`) Reference

This configuration is active on your Mac at `~/.ssh/config`:
```ssh-config
# 1. Phone VPS (OnePlus 13s - Local Node)
Host quantsync-phone
    HostName 192.168.29.163
    Port 8022
    User u0_a565
    IdentityFile ~/.ssh/id_ed25519

# 2. Cloud VPS (Ubuntu 24.04 LTS - Cloud Production Node)
Host quantsync-cloud
    HostName 51.68.204.210
    Port 20034
    User root
    IdentityFile ~/.ssh/id_ed25519

---

## 6. Services & Dynamic DNS on Cloud VPS

### Service Management Script
Start or check all production services via `tmux`:
```bash
ssh quantsync-cloud "/root/quantsync/start_quantsync_vps.sh"
```
This manages:
- `quantsync_server`: FastAPI REST API on port `8000` (serving `/lab/v1/health`, engine endpoints, Wix integration).
- `quantsync_sched`: 24/7 autonomous trading scheduler daemon (`scheduler.py --daemon`).
- `cron`: Autonomous pipelines for hourly screener snapshots, EOD pipelines, intel sweeps.

### DuckDNS Dynamic DNS
- **Domain**: `quantsync-api.duckdns.org` -> Points to `51.68.204.210`.
- **Script**: `/root/duckdns/duck.sh`
- **Cron**: Synchronizes every 5 minutes and on boot.

### Data & Ingestion Status
- **Universe Candles**: All 2,741 symbols backfilled with **2,000 calendar days** of historical bars up to current date.
- **DNA Cache**: Multiprocess DNA reports and daily screener snapshots pre-computed.
- **Intel News**: Live feeds for announcements, filings, board meetings, and global market mood active.
- **LLM Rule**: Strictly NO LLM / Ollama runs on this Cloud VPS (LLM tasks remain exclusively on Node 1 Phone VPS).

```
