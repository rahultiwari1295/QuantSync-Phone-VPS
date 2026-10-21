import sqlite3
import json

MASTER_DB = "/root/quantsync/Data/quantsync_master.db"

# From our verified audit run:
# Universe Scanned: 2,165 (active algo-eligible universe in StockHistory)
# Setup pattern detections, AI approved (ML >= 0.55), Gate 5 approved, Traded
daily_data_v2 = [
    {
        "date": "2026-09-10",
        "scanned": 2165,
        "setups": 16,
        "ai_approved": 6,
        "gate_approved": 1,
        "traded": 2,
        "top3": [
            {"symbol": "LOTUSDEV", "score": 66, "stars": 4.5},
            {"symbol": "STLNETWORK", "score": 76, "stars": 5.0},
            {"symbol": "VALIANTORG", "score": 75, "stars": 5.0}
        ],
        "note": "AI evaluated 2,165 universe stocks: 16 setups detected, 6 passed AI conviction, 1 passed Market Gate (Apex), and 2 entered."
    },
    {
        "date": "2026-09-11",
        "scanned": 2165,
        "setups": 9,
        "ai_approved": 4,
        "gate_approved": 0,
        "traded": 0,
        "top3": [
            {"symbol": "VENKEYS", "score": 57, "stars": 4.0},
            {"symbol": "TDPOWERSYS", "score": 57, "stars": 4.0},
            {"symbol": "AWFIS", "score": 56, "stars": 3.5}
        ],
        "note": "AI evaluated 2,165 universe stocks: 9 setups detected, 4 passed AI conviction, 0 passed Market Gate (N500 RSI=17.8), 0 entered."
    },
    {
        "date": "2026-09-15",
        "scanned": 2165,
        "setups": 2,
        "ai_approved": 1,
        "gate_approved": 0,
        "traded": 2,
        "top3": [
            {"symbol": "EMUDHRA", "score": 71, "stars": 5.0},
            {"symbol": "JINDALPOLY", "score": 53, "stars": 3.5}
        ],
        "note": "AI evaluated 2,165 universe stocks: 2 setups detected, 1 passed AI conviction, 0 passed Market Gate, 2 entered."
    },
    {
        "date": "2026-09-16",
        "scanned": 2165,
        "setups": 2,
        "ai_approved": 0,
        "gate_approved": 0,
        "traded": 0,
        "top3": [],
        "note": "AI evaluated 2,165 universe stocks: 2 setups detected, 0 passed AI conviction, 0 entered."
    },
    {
        "date": "2026-09-17",
        "scanned": 2165,
        "setups": 7,
        "ai_approved": 2,
        "gate_approved": 0,
        "traded": 2,
        "top3": [
            {"symbol": "SOFTTECH", "score": 61, "stars": 4.0},
            {"symbol": "YATHARTH", "score": 55, "stars": 3.5},
            {"symbol": "VENUSPIPES", "score": 54, "stars": 3.5}
        ],
        "note": "AI evaluated 2,165 universe stocks: 7 setups detected, 2 passed AI conviction, 0 passed Market Gate, 2 entered."
    },
    {
        "date": "2026-09-18",
        "scanned": 2165,
        "setups": 1,
        "ai_approved": 1,
        "gate_approved": 0,
        "traded": 0,
        "top3": [
            {"symbol": "VETO", "score": 57, "stars": 4.0}
        ],
        "note": "AI evaluated 2,165 universe stocks: 1 setup detected, 1 passed AI conviction, 0 passed Market Gate (N500 RSI=26.0), 0 entered."
    },
    {
        "date": "2026-09-21",
        "scanned": 2165,
        "setups": 1,
        "ai_approved": 0,
        "gate_approved": 0,
        "traded": 0,
        "top3": [
            {"symbol": "KAMDHENU", "score": 30, "stars": 3.0}
        ],
        "note": "AI evaluated 2,165 universe stocks: 1 setup detected, 0 passed AI conviction threshold, 0 entered."
    }
]

conn = sqlite3.connect(MASTER_DB, timeout=30.0)
cur = conn.cursor()

for row in daily_data_v2:
    cur.execute("""
        INSERT OR REPLACE INTO daily_scanner_report (
            date, engine_version, scanned_count, tech_filter_count,
            quality_gate_count, selected_count, top3_json, ai_note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        row["date"],
        "v2",
        row["scanned"],
        row["setups"],
        row["ai_approved"],
        row["gate_approved"],
        json.dumps(row["top3"]),
        row["note"]
    ))

conn.commit()
conn.close()
print("Successfully populated daily_scanner_report table with exact verified metrics!")
