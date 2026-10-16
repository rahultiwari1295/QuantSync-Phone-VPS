# QuantSync Local VPS Guide (OnePlus 13s)

Complete technical guide, architecture breakdown, operational workflows, and connection manual for running the **QuantSync Production Node** on a OnePlus 13s (`CPH2723`).

---

## 1. System Specifications & Architecture

* **Hardware**: OnePlus 13 (`CPH2723`)
* **CPU**: Qualcomm Snapdragon 8 Elite (Oryon `aarch64` 64-bit architecture)
* **RAM**: 12 GB / 16 GB LPDDR5X
* **Storage Available**: ~21 GB free on UFS 4.0 internal flash (`/data`)
* **Host Layer**: Termux (Android sandboxed environment)
* **Container Layer**: Ubuntu 26.04 LTS (`proot-distro` aarch64 container)
* **Python Runtime**: Python 3.14 (with pre-compiled ARM64 `manylinux` wheels)
* **Core Libraries**: `numpy 2.5.4`, `pandas 3.0.6`, `duckdb 1.5.6`, `pyarrow 26.0.0`, `xgboost 3.4.1`, `scikit-learn 1.9.1`, `lightgbm 4.7.0`, `scipy 1.18.1`, `fastapi 0.143.1`, `uvicorn 0.54.0`

### Architecture Diagram
```
                     Market Hours (09:15 - 15:30 IST)
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   ONEPLUS 13s PRODUCTION VPS NODE                      │
│                                                                        │
│   [Outbound Angel One SmartAPI] ──▶ Real-time Quotes & Candles        │
│                                           │                            │
│   [Ubuntu Container: /root/quantsync]     │                            │
│   ├── Data/stock_history.db (226 MB) ─────┼───▶ RSI & Indicators       │
│   ├── Data/stock_dna_cache.db (157 MB) ───┤                            │
│   ├── engines/v2 (Daily Swing Setup) ─────┤───▶ Trailing Stop Monitor  │
│   ├── engines/v3 (15m Intraday Candle) ───┤                            │
│   └── engines/v4 (98k Fingerprints & ML) ─┘───▶ Win & Return Scoring   │
│                                           │                            │
│   [1-Minute Snapshot Generation] ─────────┘                            │
│   ├── Evaluates Active & Closed Trades                                 │
│   └── Bundles to 1 compressed JSON document (~120 KB)                  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Outbound HTTPS (1 write / min)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                 GOOGLE FIREBASE FIRESTORE (Spark Free Plan)            │
│   • Document: snapshots/live_intraday (75–375 writes/day of 20,000)   │
│   • Document: snapshots/daily_v2 & stock_dna (EOD at 15:45 IST)        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Live Streamed Reads
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  PUBLIC TRADING WEBSITE (Vercel Free Tier)             │
│   • rsitradingplatform.vercel.app (/app & /m)                          │
│   • Reads edge cache: Zero load on phone, zero crash risk, ₹0 cost     │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Network & Connection Details

### Network Information
* **Local Wi-Fi IP**: `192.168.29.163` (JioFiber / Home Network)
* **SSH Port**: `8022`
* **FastAPI Local Port**: `8000` (Internal only)
* **Authentication**: Passwordless via Mac SSH Key (`~/.ssh/id_ed25519`)
* **Security Model**: Zero public internet inbound ports. Completely private behind home router firewall.

---

## 3. How to Connect from Your Mac

### Method A: Over ADB (When plugged into USB or Wireless ADB is Active)
```bash
# 1. Forward port (one time)
adb forward tcp:8022 tcp:8022

# 2. Connect via SSH
ssh -p 8022 localhost
```

### Method B: Over Home Wi-Fi (No Cable, Direct Wi-Fi)
```bash
# Connect directly to phone's Wi-Fi IP
ssh -p 8022 192.168.29.163
```

### Method C: One-Line Ubuntu Entry
To jump straight into the Ubuntu environment from your Mac in one command:
```bash
ssh -p 8022 localhost "proot-distro login ubuntu"
```

---

## 4. Key Directory Paths on the Phone

| Component | Path on OnePlus 13s |
| :--- | :--- |
| **QuantSync Project Root** | `/root/quantsync` *(inside Ubuntu)*<br>`/data/data/com.termux/files/home/quantsync` *(in Termux)* |
| **Python Virtual Environment** | `/root/qs_env` *(inside Ubuntu)* |
| **Historical Candle Store** | `/root/quantsync/Data/stock_history.db` (226 MB) |
| **Stock DNA Cache** | `/root/quantsync/Data/stock_dna_cache.db` (157 MB) |
| **Master Trade Database** | `/root/quantsync/Data/quantsync_master.db` (18 MB) |
| **V2 Frozen Swing Model** | `/root/quantsync/daily_v2/models/frozen_daily_v2_model.json` |
| **V4 ML Embeddings & Models** | `/root/quantsync/models/` (24 MB embeddings, XGBoost/LGBM models) |

---

## 5. Development & Sync Workflow (Mac ➔ Phone)

You do **not** code on the phone. All development happens on your Mac in `/Users/rahultiwari/Projects/WIX_with_local/wix_latest_code/QuantSync/vps_data_phone`.

### 1. Pushing Code Changes to Phone (< 2 Seconds)
Whenever you modify Python engines, scanner files, or configs on your Mac, push only the changes using:
```bash
rsync -avz -e 'ssh -p 8022' --exclude 'Data/*.db' \
  /Users/rahultiwari/Projects/WIX_with_local/wix_latest_code/QuantSync/vps_data_phone/ localhost:~/quantsync/
```
*(The `--exclude 'Data/*.db'` flag guarantees the heavy database files are not needlessly re-transferred).*

### 2. Running Verification & Health Checks
Run the full 7-point production pre-flight readiness check remotely:
```bash
ssh -p 8022 localhost "proot-distro login ubuntu -- bash -c 'cd /root/quantsync && python3 check_health.py'"
```

### 3. Running Services in the Background (Continuous Mode)
To launch a daemon that continues running even if your Mac disconnects:
```bash
ssh -p 8022 localhost "proot-distro login ubuntu -- bash -c 'cd /root/quantsync && nohup python3 scheduler.py > scheduler.log 2>&1 &'"
```

### 4. Viewing Real-Time Logs on Phone
```bash
ssh -p 8022 localhost "proot-distro login ubuntu -- tail -f /root/quantsync/scheduler.log"
```

---

## 6. Phone Anti-Sleep & Process Retention Safeguards

Android power saving rules have been disabled for Termux. If you ever update OxygenOS or reboot the phone, verify these settings:

### 1. In Android Settings (On Phone)
* **Settings → Apps → Termux → Battery**: Set to **"Unrestricted"**.
* **Recent Apps Screen**: Tap the three dots above Termux and choose **"Lock"** (prevents swipe-to-close).
* **Termux Notification**: Pull down notification bar and tap **"Acquire WakeLock"**.

### 2. Via ADB (Permanent Android Engine Optimizations)
Run these two commands once if the phone is ever factory reset or receives a major Android OS upgrade:
```bash
# Disable Android 32-process Phantom Process Killer
adb shell "/system/bin/device_config put activity_manager max_phantom_processes 2147483647"

# Whitelist Termux from device idle/doze mode
adb shell "dumpsys deviceidle whitelist +com.termux"
```

---

## 7. Permanent Public Ingress & Fixed URL

The OnePlus 13s runs an **Ngrok v3 tunnel** configured with a static, permanent domain that never changes across reboots:

* **Fixed Public URL**: `https://backing-lapping-denial.ngrok-free.dev`
* **Static Target**: Forwards securely to `http://localhost:8000` on the phone.

### Live Production Endpoints (Tested & Verified)

| Feature / Screen | Public Route | Response Type |
| :--- | :--- | :--- |
| **QuantSync Trading Terminal** | `https://backing-lapping-denial.ngrok-free.dev/dashboard` | Interactive UI |
| **Stock DNA Citadel (Mobile Story)** | `https://backing-lapping-denial.ngrok-free.dev/dna` | Interactive UI |
| **Stock DNA Symbol Search** | `https://backing-lapping-denial.ngrok-free.dev/api/dna/search?q=TATA` | JSON (Real-time) |
| **Hot Institutional Radar** | `https://backing-lapping-denial.ngrok-free.dev/api/dna/discovery` | JSON (Real-time) |
| **Deep Stock DNA Report** | `https://backing-lapping-denial.ngrok-free.dev/api/dna/report/TATAMOTORS` | JSON (Calculated / Cached) |
| **V4 Institutional Terminal Feed** | `https://backing-lapping-denial.ngrok-free.dev/api/v4/terminalData` | JSON (RAM Cached Snapshot) |
| **Health Check Endpoint** | `https://backing-lapping-denial.ngrok-free.dev/health` | JSON |
| **Screener & Scanner** | `https://backing-lapping-denial.ngrok-free.dev/screener` | Interactive UI |

> [!NOTE]
> **Ngrok Free Tier Note**:
> When opening the link in a desktop or mobile browser for the first time, click **"Visit Site"** once to pass the initial ngrok interstitial.
> For programmatic API calls (from Wix frontend, cURL, or apps), simply pass the HTTP header:
> `ngrok-skip-browser-warning: true`

---

## 8. Autonomous Supervisor & Recovery (`start_quantsync.sh`)

An autonomous watchdog script is installed at `~/start_quantsync.sh` and linked to `~/.bashrc` & `~/.bash_profile`.

### What it does:
1. Verifies if `quantsync_server` is running in tmux inside proot Ubuntu (`0.0.0.0:8000`). If not, starts it.
2. Verifies if `quantsync_ngrok` is running with static URL `https://backing-lapping-denial.ngrok-free.dev`. If not, starts it.

### If Phone Reboots:
1. Open the **Termux** app on the phone.
2. The startup script runs automatically, or you can run:
   ```bash
   ~/start_quantsync.sh
   ```
3. Tap **"Acquire WakeLock"** in the Android notification shade.
Both the backend trading engines and the public URL will immediately be back online with zero configuration required!

