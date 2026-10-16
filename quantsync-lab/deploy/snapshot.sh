#!/bin/sh
# Consistent copy of the live candle DB for the Lab (runs as root before each cook).
set -e
rm -f /opt/quantsync-lab/data/stock_history.snapshot.db
sqlite3 /root/quantsync/Data/stock_history.db ".backup '/opt/quantsync-lab/data/stock_history.snapshot.db'"
chown qslab:qslab /opt/quantsync-lab/data/stock_history.snapshot.db
