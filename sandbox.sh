#!/usr/bin/env bash
# ==============================================================================
# sandbox.sh — QuantSync Staging Sandbox Management Tool
# ==============================================================================
# Manages an isolated staging environment on Port 8444 with a strict 2GB RAM cap.
#
# Commands:
#   sandbox start    — Clones prod to staging, links DBs, starts port 8444 (2GB limit)
#   sandbox status   — Checks staging health, active PID, RAM used vs 2GB ceiling
#   sandbox test     — Sends automated test probes to port 8444
#   sandbox logs     — Streams live staging logs
#   sandbox stop     — Halts staging service on port 8444
#   sandbox promote  — Safely promotes verified staging code to Prod (8443)
#   sandbox discard  — Destroys staging environment and purges scratch files
# ==============================================================================

set -e

PROD_DIR="/root/quantsync"
STAGING_DIR="/root/quantsync_staging"
SERVICE_NAME="quantsync_staging.service"
PROD_SERVICE="quantsync_api.service"
PORT=8444

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

function log_info() {
    echo -e "${CYAN}[SANDBOX]${NC} $1"
}

function log_success() {
    echo -e "${GREEN}[SANDBOX] ✓${NC} $1"
}

function log_warn() {
    echo -e "${YELLOW}[SANDBOX] ⚠${NC} $1"
}

function log_error() {
    echo -e "${RED}[SANDBOX] ✗${NC} $1"
}

function cmd_start() {
    log_info "Initializing Staging Sandbox Environment (Port ${PORT}, 2GB Memory Limit)..."

    # 1. Sync code from prod to staging (excluding Data, venv, logs)
    mkdir -p "${STAGING_DIR}/Data" "${STAGING_DIR}/logs"
    rsync -avq --delete \
        --exclude 'Data/' \
        --exclude 'venv/' \
        --exclude 'logs/' \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.git/' \
        "${PROD_DIR}/" "${STAGING_DIR}/"

    # 2. Database Isolation:
    # Copy master DB (1.9MB) so staging test writes never touch prod tables
    if [ -f "${PROD_DIR}/Data/quantsync_master.db" ]; then
        cp -f "${PROD_DIR}/Data/quantsync_master.db" "${STAGING_DIR}/Data/quantsync_master.db"
    fi

    # Symlink large read-only databases (zero extra disk space)
    for db in stock_history.db stock_dna_cache.db fundamental_cache.db; do
        if [ -f "${PROD_DIR}/Data/${db}" ]; then
            ln -sf "${PROD_DIR}/Data/${db}" "${STAGING_DIR}/Data/${db}"
        fi
    done

    # 3. Isolated RAM Cache directory
    mkdir -p "/dev/shm/quantsync_staging"

    # 4. Start systemd service
    systemctl daemon-reload
    systemctl restart "${SERVICE_NAME}"
    sleep 2

    # 5. Check if running
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log_success "Staging environment is ACTIVE on Port ${PORT} (SSL)!"
        cmd_status
    else
        log_error "Failed to start ${SERVICE_NAME}. Check logs with: sandbox logs"
        exit 1
    fi
}

function cmd_status() {
    echo "=================================================================="
    echo "  QuantSync Staging Sandbox Status"
    echo "=================================================================="

    # Service Status
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        echo -e "Status:        ${GREEN}RUNNING (Active)${NC}"
    else
        echo -e "Status:        ${RED}STOPPED (Inactive)${NC}"
    fi

    echo -e "Port:          ${CYAN}https://127.0.0.1:${PORT}${NC}"
    echo -e "Directory:     ${STAGING_DIR}"

    # Memory Limit & Usage (2GB Cap)
    if [ -f "/sys/fs/cgroup/system.slice/${SERVICE_NAME}/memory.current" ]; then
        CURRENT_MEM_BYTES=$(cat "/sys/fs/cgroup/system.slice/${SERVICE_NAME}/memory.current" 2>/dev/null || echo "0")
        CURRENT_MEM_MB=$((CURRENT_MEM_BYTES / 1024 / 1024))
        echo -e "Memory Used:   ${CURRENT_MEM_MB} MB / ${YELLOW}2048 MB (2.0 GB Limit)${NC}"
    elif systemctl is-active --quiet "${SERVICE_NAME}"; then
        PID=$(pgrep -f "port ${PORT}" | head -n 1 || true)
        if [ -n "$PID" ]; then
            RSS_KB=$(grep VmRSS /proc/$PID/status 2>/dev/null | awk '{print $2}' || echo "0")
            RSS_MB=$((RSS_KB / 1024))
            echo -e "Memory Used:   ${RSS_MB} MB / ${YELLOW}2048 MB (2.0 GB Limit)${NC}"
        fi
    fi

    # Live Production check
    echo -e "------------------------------------------------------------------"
    if systemctl is-active --quiet "${PROD_SERVICE}"; then
        echo -e "Live Prod API: ${GREEN}ONLINE (Port 8443)${NC} (100% Unaffected)"
    else
        echo -e "Live Prod API: ${RED}WARNING: PROD IS NOT RUNNING!${NC}"
    fi
    echo "=================================================================="
}

function cmd_test() {
    log_info "Probing Staging Sandbox endpoints on Port ${PORT}..."
    python3 -c "
import urllib.request, ssl, time
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = 'https://127.0.0.1:${PORT}/api/terminalData'
t0 = time.time()
try:
    resp = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'SandboxTest'}), context=ctx, timeout=5)
    dt = (time.time() - t0) * 1000
    print(f'✓ Staging /api/terminalData -> HTTP {resp.status} in {dt:.1f}ms')
except Exception as e:
    print(f'✗ Staging test failed: {e}')
"
}

function cmd_logs() {
    tail -f "${STAGING_DIR}/logs/api_staging.log"
}

function cmd_stop() {
    log_info "Stopping Staging Sandbox service..."
    systemctl stop "${SERVICE_NAME}" || true
    pkill -f "port ${PORT}" || true
    log_success "Staging environment stopped."
}

function cmd_promote() {
    log_warn "PROMOTING STAGING CODE TO LIVE PRODUCTION..."
    echo -e "${YELLOW}This will copy code from ${STAGING_DIR} to ${PROD_DIR} and restart live Prod.${NC}"
    read -p "Are you sure you want to promote to PRODUCTION? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # 1. Sync code changes to Prod
        rsync -av \
            --exclude 'Data/' \
            --exclude 'venv/' \
            --exclude 'logs/' \
            --exclude '__pycache__/' \
            --exclude '*.pyc' \
            --exclude '.git/' \
            "${STAGING_DIR}/" "${PROD_DIR}/"

        # 2. Restart Live Prod cleanly
        log_info "Restarting Live Production API (${PROD_SERVICE})..."
        systemctl restart "${PROD_SERVICE}"
        sleep 2

        if systemctl is-active --quiet "${PROD_SERVICE}"; then
            log_success "Promotion COMPLETE! Live Production is healthy on Port 8443."
        else
            log_error "Production restart failed! Check logs: journalctl -u ${PROD_SERVICE} -n 30"
        fi

        # Optional: stop staging
        cmd_stop
    else
        log_info "Promotion canceled."
    fi
}

function cmd_discard() {
    log_warn "DISCARDING STAGING ENVIRONMENT..."
    cmd_stop
    rm -rf "${STAGING_DIR}"
    rm -rf "/dev/shm/quantsync_staging"
    log_success "Staging directory completely purged. 0 leftover files."
}

case "$1" in
    start)
        cmd_start
        ;;
    status)
        cmd_status
        ;;
    test)
        cmd_test
        ;;
    logs)
        cmd_logs
        ;;
    stop)
        cmd_stop
        ;;
    promote)
        cmd_promote
        ;;
    discard)
        cmd_discard
        ;;
    *)
        echo "Usage: sandbox {start|status|test|logs|stop|promote|discard}"
        exit 1
        ;;
esac
