# tin-trades

Pre-market data pipeline for SPY, QQQ, IWM and MAG7. Collects options chains, computes gamma exposure (GEX), tracks earnings and macro events, and pushes a daily brief to GitHub before market open.

---

## What it does

- Fetches **options chains** (OI, IV, bid/ask, volume) for SPY, QQQ, IWM + MAG7 at 9:00 and 9:35 AM ET
- Computes **GEX per strike** via Black-Scholes — identifies wall, support, resistance, and gamma environment (positive/negative)
- Pulls **earnings calendar** (NASDAQ API, filtered by OI / market cap / analyst estimates)
- Pulls **macro events** (ForexFactory, high-impact USD only)
- Generates a **daily brief** (`gex.html` + `DAILY.md`) with a 2×5 GEX grid and today's earnings/events — auto-committed and pushed to GitHub at 9:20 AM ET
- Stores **1m OHLCV candles** after market close
- Runs a **Telegram bot** for on-demand `/earnings` and `/events` queries
- Serves a **local dashboard** at `localhost:8000` with live chain, GEX grid, and earnings/events tables

---

## Scripts

| Script | Schedule | What |
|--------|----------|------|
| `daily_report.py` | Daily, 9:20 AM ET | Computes GEX for all 10 tickers → `gex.html` + `DAILY.md` → git push |
| `options_data.py` | Daily, 9:00 AM ET | Full chain snapshot — OI, IV, volume for watchlist + MAG7 |
| `options_data.py --quotes` | Daily, 9:35 AM ET | Patch bid/ask only (faster, post-open) |
| `market_data.py` | Daily, 4:30 PM ET | 1m OHLCV candles for watchlist tickers |
| `earnings.py` | Sunday, 8:00 AM | Weekly earnings calendar |
| `events.py` | Sunday, 8:00 AM | Weekly macro events |
| `bot.py` | Always on | Telegram bot polling |

---

## Automation (launchctl)

All scripts run via macOS `launchctl`. Plists live in `~/Library/LaunchAgents/`.

| Plist | Schedule |
|-------|----------|
| `com.tintrades.daily` | Daily 6:20 AM PT (9:20 ET) |
| `com.tintrades.optionsdata` | Daily 6:00 + 6:35 AM PT |
| `com.tintrades.marketdata` | Daily 1:30 PM PT |
| `com.tintrades.weekly` | Sunday 8:00 AM PT |
| `com.tintrades.bot` | Always on (KeepAlive) |

```bash
# load all
launchctl load ~/Library/LaunchAgents/com.tintrades.*.plist

# check status
launchctl list | grep tintrades

# unload all
launchctl unload ~/Library/LaunchAgents/com.tintrades.*.plist
```

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
```

---

## Config (`config.yaml`)

```yaml
watchlist:            # tickers for candles + options chains
  - SPY
  - QQQ
  - IWM

mag7:                 # MAG7 for options chains + GEX only (no candles)
  - AAPL
  - NVDA
  - MSFT
  - AMZN
  - GOOGL
  - META
  - TSLA

prepost: true         # include pre/post market in 1m candles

options:
  strikes_n: 2        # strikes each side of ATM
  chain_expiries: 2   # 0DTE + next Friday

earnings:
  oi_min:  5000       # minimum total options OI
  cap_min: 10000000000  # minimum market cap ($10B)
  est_min: 4          # minimum analyst estimates
```

---

## CLI usage

```bash
# daily brief (skip if market closed; --force to override)
.venv/bin/python daily_report.py
.venv/bin/python daily_report.py --force

# options chain
.venv/bin/python options_data.py           # 9:00 AM — full chain
.venv/bin/python options_data.py --quotes  # 9:35 AM — bid/ask patch

# candles
.venv/bin/python market_data.py

# earnings
.venv/bin/python earnings.py               # this week
.venv/bin/python earnings.py --next        # next week
.venv/bin/python earnings.py --week 27     # specific ISO week
.venv/bin/python earnings.py --weeks 2     # span 2 weeks
.venv/bin/python earnings.py --all         # no filter

# events
.venv/bin/python events.py                 # this week
.venv/bin/python events.py --next          # next week

# local dashboard
.venv/bin/python test/server.py            # → http://localhost:8000

# Telegram bot
set -a && source .env && set +a
.venv/bin/python bot.py
```

---

## Telegram bot commands

```
/earnings          this week
/earnings today    today only
/earnings next     next week
/events            this week
/events next       next week
```

---

## Data

```
data/
  earnings.db                                SQLite — weekly earnings calendar
  events.db                                  SQLite — macro events
  candles/{ticker}_{year}.parquet            1m OHLCV (datetime, open, high, low, close, volume)
  options/{ticker}_chain_{year}.parquet      daily chain snapshot
  gamma/{ticker}_gex_{year}.parquet          GEX by strike and expiry
  gamma/daily_summary.json                   today's computed GEX summary (all tickers)
```

**Options chain columns:**
`date, expiry, strike, call_oi, call_iv, call_bid, call_ask, call_volume, put_oi, put_iv, put_bid, put_ask, put_volume`

**GEX columns:**
`date, expiry, strike, gex`

---

## Daily brief (`gex.html`)

Generated each morning at 9:20 ET and pushed to GitHub. Contains:

- **Earnings today** — symbol, name, report time, EPS estimate, market cap
- **Events today** — time, title, forecast, previous
- **2×5 GEX grid** — one tile per ticker (SPY, QQQ, IWM, AAPL, NVDA, MSFT, AMZN, GOOGL, META, TSLA)
  - Bars: magenta = positive GEX (pinning), cyan = negative GEX (volatile)
  - Gold = wall (highest abs GEX strike)
  - ▲ RES / ▼ SUPP = key levels above/below spot
  - Footer: net GEX, POS/NEG environment

Enable GitHub Pages (repo Settings → Pages → `main` branch, `/root`) to view `gex.html` online.

---

## Logs

```
logs/
  daily.log       daily_report.py
  tin_trades.log  options_data.py + market_data.py
  weekly.log      earnings.py + events.py
  bot.log         bot.py
```
