#!/bin/bash
# Nightly 02:00 Backup of 3 databases from Cloud VPS to Phone VPS
BACKUP_DIR="/data/data/com.termux/files/home/quantsync/Data/backups"
LOG="$BACKUP_DIR/backup.log"
mkdir -p "$BACKUP_DIR"
TS=$(date "+%Y-%m-%d %H:%M:%S")
echo "[$TS] Starting nightly backup from Cloud VPS (51.68.204.210)..." >> "$LOG"

FAIL=0
for DB in "/opt/quantsync-lab/db/lab.db" "/root/quantsync/Data/quantsync_master.db" "/root/quantsync/Data/intel.db"; do
    rsync -avz -e "ssh -p 20034 -o StrictHostKeyChecking=no" "root@51.68.204.210:$DB" "$BACKUP_DIR/" >> "$LOG" 2>&1
    if [ $? -ne 0 ]; then
        FAIL=1
    fi
done

TS=$(date "+%Y-%m-%d %H:%M:%S")
if [ $FAIL -eq 0 ]; then
    echo "[$TS] All 3 databases successfully backed up!" >> "$LOG"
else
    echo "[$TS] Backup completed with errors." >> "$LOG"
fi
