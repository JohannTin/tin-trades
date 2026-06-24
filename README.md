# tin-trades

Pre-market data pipeline for SPY, QQQ, IWM and MAG7.

- Options chains, GEX (Black-Scholes), earnings, macro events, 1m candles
- Daily brief (`gex.html` + `DAILY.md`) auto-pushed to GitHub at 09:20 ET
- 2×5 GEX grid — wall, support, resistance per ticker — positive/negative env
- Local dashboard (`localhost:8000`) + Telegram bot for live access
- Fully automated via launchctl (macOS)

---

## Scripts

| Script | When | What |
|--------|------|------|
| `daily_report.py` | Daily, 9:20 AM ET | GEX for 10 tickers → `gex.html` + `DAILY.md` → git push |
| `options_data.py` | Daily, 9:00 AM ET | Full chain snapshot — OI, IV, volume |
| `options_data.py --quotes` | Daily, 9:35 AM ET | Patch bid/ask after open |
| `market_data.py` | Daily, market close | 1m OHLCV candles |
| `earnings.py` | Sunday | Weekly earnings calendar filtered by OI / market cap |
| `events.py` | Sunday | High-impact USD macro events (ForexFactory) |
| `bot.py` | Always on | Telegram bot — `/earnings`, `/events` |

---

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
```

Load automation:
```bash
launchctl load ~/Library/LaunchAgents/com.tintrades.*.plist
launchctl list | grep tintrades
```

Run daily report manually:
```bash
.venv/bin/python daily_report.py --force
```

---

## Data

```
data/
  earnings.db                              SQLite — weekly earnings
  events.db                                SQLite — macro events
  candles/{ticker}_{year}.parquet          1m OHLCV
  options/{ticker}_chain_{year}.parquet    daily chain (OI, IV, bid/ask)
  gamma/{ticker}_gex_{year}.parquet        GEX by strike/expiry
  gamma/daily_summary.json                 today's GEX summary for all tickers
```
