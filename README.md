# tin-trades

Data collection pipeline for SPY, QQQ, IWM, and VIX. Earnings calendar, macro events, 1m candles, options chain snapshots, and gamma exposure — built for backtesting and daily market prep.

---

<details>
<summary><strong>Scripts</strong></summary>

| Script | When | What |
|--------|------|------|
| `earnings.py` | Sunday | Weekly earnings calendar filtered by OI / market cap |
| `events.py` | Sunday | Weekly macro events — high-impact USD only (ForexFactory) |
| `market_data.py` | Daily, 4:30 PM ET | 1m OHLCV candles for watchlist + VIX |
| `options_data.py` | Daily, 9:00 AM ET | Full chain snapshot — OI, IV, volume |
| `options_data.py --quotes` | Daily, 9:35 AM ET | Patch bid/ask after open |
| `gamma_exposure.py` | Daily, 9:05 AM ET | GEX snapshot for all watchlist tickers via IBKR TWS |
| `bot.py` | Always on | Telegram bot — `/earnings`, `/events`, `/gamma` commands |

</details>

---

<details>
<summary><strong>Data</strong></summary>

```
data/
  earnings.db                              SQLite — weekly earnings calendar
  events.db                                SQLite — weekly macro events
  candles/{ticker}_{year}.parquet          1m OHLCV per ticker per year (SPY, QQQ, IWM, ^VIX)
  options/{ticker}_chain_{year}.parquet    daily chain snapshot (OI, IV, bid/ask)
  gamma/{ticker}_gex_{year}.parquet        daily GEX detail per ticker
  gamma/{ticker}_summary.json             latest GEX summary per ticker
```

**Chain columns:** `date, expiry, strike, call_oi, call_iv, call_bid, call_ask, call_volume, put_oi, put_iv, put_bid, put_ask, put_volume`

</details>

---

<details>
<summary><strong>Setup</strong></summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

</details>

---

<details>
<summary><strong>Config (<code>config.yaml</code>)</strong></summary>

```yaml
watchlist: [SPY, QQQ, IWM]   # options chains + GEX
price_only: [^VIX]            # 1m candles only, no options

prepost: true

ibkr:
  host: 127.0.0.1
  port: 7497
  client_id: 10
  readonly: true
  gex_strikes_pct: 0.08       # strikes within ±8% of spot

options:
  strikes_n: 2
  chain_expiries: 2

earnings:
  oi_min:  5000
  cap_min: 10000000000
  est_min: 4
```

</details>

---

<details>
<summary><strong>CLI flags</strong></summary>

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

</details>

---

<details>
<summary><strong>Telegram bot</strong></summary>

```
/gamma             SPY gamma exposure (from daily snapshot)
/gamma QQQ         QQQ gamma (snapshot if available, else live)
/gamma NVDA        live GEX for any ticker
/alert TICKER      alert when wall breaks or +GEX zone hit
/alert off         clear all alerts
/earnings          this week
/earnings today    today only
/earnings next     next week
/events            this week
/events next       next week
```

Run: `set -a && source .env && set +a && .venv/bin/python bot.py`

</details>

---

<details>
<summary><strong>Automation (launchctl)</strong></summary>

| Plist | Schedule | What |
|-------|----------|------|
| `com.tintrades.bot` | Always on (KeepAlive) | Telegram bot |
| `com.tintrades.weekly` | Sunday 8:00 AM PT | Runs `run_weekly.sh` — fetches earnings + events |
| `com.tintrades.optionsdata` | 6:00 AM + 6:35 AM PT | Options chain snapshot + bid/ask patch |
| `com.tintrades.gamma` | 6:05 AM PT (9:05 AM ET) | GEX snapshot for SPY, QQQ, IWM |
| `com.tintrades.marketdata` | 1:30 PM PT (4:30 PM ET) | 1m OHLCV candles |

```bash
launchctl load ~/Library/LaunchAgents/com.tintrades.weekly.plist
launchctl list | grep tintrades
```

Logs: `logs/tin_trades.log`, `logs/bot.log`, `logs/gamma.log`, `logs/options.log`

</details>
