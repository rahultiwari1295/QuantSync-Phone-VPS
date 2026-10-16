# DEPRECATION NOTICE: `daily_v2/` is Deprecated

> **Date:** October 2026
> **Status:** ARCHIVED & DEPRECATED
> **Authoritative Replacement:** `vps_data_live/engines/v2/` and `vps_data_live/daily_processor.py`

### Rationale:
The `daily_v2/` folder was a legacy prototype containing 9 strategies writing to an isolated `daily_v2_production.db` and separate tables in `stock_history.db`. 

All production trading operations, scheduler tasks, and Wix synchronization for **QuantSync-V2** are consolidated exclusively on:
1. **Engine Class:** `vps_data_live/engines/v2/engine.py` (`QuantSyncV2Engine`)
2. **Scanner:** `vps_data_live/engines/v2/live_scanner.py` (`V11LiveScanner`)
3. **Daily Processor:** `vps_data_live/daily_processor.py` (`process_daily`)
4. **Master Storage:** `Data/quantsync_master.db` (`MasterStorage`)

Do not import or execute modules inside `daily_v2/`.
