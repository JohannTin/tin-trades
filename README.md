# tin-trades

Data collection pipeline for SPY, QQQ, IWM. Earnings calendar, macro events, 1m candles, and options chain snapshots — built for backtesting and daily market prep.

---

## Scripts

| Script | When | What |
|--------|------|------|
| `earnings.py` | Sunday | Weekly earnings calendar filtered by OI / market cap |
| `events.py` | Sunday | Weekly macro events — high-impact USD only (ForexFactory) |
| `market_data.py` | Daily, 4:30 PM ET | 1m OHLCV candles for watchlist |
| `options_data.py` | Daily, 9:00 AM ET | Full chain snapshot — OI, IV, volume |
| `options_data.py --quotes` | Daily, 9:35 AM ET | Patch bid/ask after open |
| `bot.py` | Always on | Telegram bot — `/earnings`, `/events` commands |

---

## Data

```
data/
  earnings.db                          SQLite — weekly earnings calendar
  events.db                            SQLite — weekly macro events
  candles/{ticker}_{year}.parquet      1m OHLCV per ticker per year
  options/{ticker}_chain_{year}.parquet  daily chain snapshot (OI, IV, bid/ask)
```

**Chain columns:** `date, expiry, strike, call_oi, call_iv, call_bid, call_ask, call_volume, put_oi, put_iv, put_bid, put_ask, put_volume`

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` to enable the bot and Telegram notifications.

---

## Config (`config.yaml`)

```yaml
watchlist: [SPY, QQQ, IWM]
prepost: true          # include pre/post market candles

options:
  strikes_n: 2         # strikes each side of ATM for 1m candles
  chain_expiries: 2    # 0DTE + friday

earnings:
  oi_min:  5000        # minimum total options OI
  cap_min: 10000000000 # minimum market cap ($10B)
  est_min: 4           # minimum analyst estimates
```

---

## CLI flags

```bash
# earnings.py
.venv/bin/python earnings.py               # this week
.venv/bin/python earnings.py --next        # next week
.venv/bin/python earnings.py --week 27     # specific ISO week
.venv/bin/python earnings.py --weeks 2     # span 2 weeks
.venv/bin/python earnings.py --all         # no filter

# events.py
.venv/bin/python events.py                 # this week
.venv/bin/python events.py --next          # next week
```

---

## Telegram bot

```
/earnings          this week
/earnings today    today only
/earnings next     next week
/events            this week
/events next       next week
```

Run: `set -a && source .env && set +a && .venv/bin/python bot.py`

---

## Automation (launchctl)

| Plist | Schedule | What |
|-------|----------|------|
| `com.tintrades.bot` | Always on (KeepAlive) | Telegram bot |
| `com.tintrades.weekly` | Sunday 8:00 AM PT | Runs `run_weekly.sh` — fetches earnings + events for next week |
| `com.tintrades.optionsdata` | 6:00 AM + 6:35 AM PT (9:00 + 9:35 AM ET) | Options chain snapshot + bid/ask patch |
| `com.tintrades.marketdata` | 1:30 PM PT (4:30 PM ET) | 1m OHLCV candles |

```bash
# load / unload
launchctl load ~/Library/LaunchAgents/com.tintrades.weekly.plist
launchctl unload ~/Library/LaunchAgents/com.tintrades.weekly.plist

# run weekly fetch manually
./run_weekly.sh

# check status
launchctl list | grep tintrades
```

Logs: `logs/weekly.log`, `logs/bot.log`
