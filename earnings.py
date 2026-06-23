#!/usr/bin/env python3
"""
Weekly earnings calendar — notable companies only.

Filters: OI >= 5,000  OR  (market cap >= $10B AND analyst estimates >= 4)
Sorted by total options open interest descending.

Usage:
    .venv/bin/python earnings.py                # this week
    .venv/bin/python earnings.py --next         # next week
    .venv/bin/python earnings.py --next --weeks 2  # next 2 weeks
    .venv/bin/python earnings.py --week 27      # specific ISO week
    .venv/bin/python earnings.py --week 27 --weeks 2  # week 27 + 28
    .venv/bin/python earnings.py --all          # no filter
"""
import sys
import os
import re
import sqlite3
import time
import requests
import yfinance as yf
import yaml
from datetime import date, datetime, timedelta

from utils import is_market_open, setup_logger, log_run, send_telegram

HEADERS     = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
EARNINGS_DB = 'data/earnings.db'

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)


NEXT_WEEK = '--next'  in sys.argv
SHOW_ALL  = '--all'   in sys.argv

WEEK_NUM = None
if '--week' in sys.argv:
    idx      = sys.argv.index('--week')
    WEEK_NUM = int(sys.argv[idx + 1])

WEEKS_N = 1
if '--weeks' in sys.argv:
    idx     = sys.argv.index('--weeks')
    WEEKS_N = int(sys.argv[idx + 1])

log     = setup_logger('earnings', prefix='earnings')

_ecfg   = cfg.get('earnings', {})
OI_MIN  = _ecfg.get('oi_min',  5_000)
CAP_MIN = _ecfg.get('cap_min', 10_000_000_000)
EST_MIN = _ecfg.get('est_min', 4)


# --- DB ---

def get_db():
    os.makedirs('data', exist_ok=True)
    conn = sqlite3.connect(EARNINGS_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS earnings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            year        INTEGER NOT NULL,
            week        INTEGER NOT NULL,
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            name        TEXT,
            report_time TEXT,
            eps_est     TEXT,
            market_cap  INTEGER,
            oi          INTEGER,
            num_ests    INTEGER,
            fetched_at  TEXT NOT NULL,
            UNIQUE (year, week, symbol)
        )
    ''')
    conn.commit()
    return conn


def save_earnings(results, year, week):
    conn       = get_db()
    fetched_at = datetime.now().isoformat(timespec='seconds')
    conn.executemany(
        '''INSERT OR REPLACE INTO earnings
           (year, week, date, symbol, name, report_time, eps_est,
            market_cap, oi, num_ests, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        [(year, week, r['day'], r['sym'], r['name'], r['time'],
          r['eps'], r['cap'], r['oi'], r['ests'], fetched_at)
         for r in results]
    )
    conn.commit()
    conn.close()


# --- Helpers ---

def parse_cap(s):
    return int(re.sub(r'[^\d]', '', s or '') or 0)


def fmt_cap(n):
    if n >= 1_000_000_000_000: return f'${n/1e12:.1f}T'
    if n >= 1_000_000_000:     return f'${n/1e9:.1f}B'
    if n >= 1_000_000:         return f'${n/1e6:.0f}M'
    return '-'


def fmt_time(t):
    if t == 'time-pre-market':  return 'pre'
    if t == 'time-after-hours': return 'aft'
    return ' - '


def week_trading_days(monday):
    return [monday + timedelta(days=i) for i in range(5) if is_market_open(monday + timedelta(days=i))]


def fetch_nasdaq(day):
    try:
        r = requests.get(f'https://api.nasdaq.com/api/calendar/earnings?date={day}',
                         headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json().get('data', {}).get('rows', [])
    except Exception as e:
        log.warning(f'Failed to fetch {day}: {e}')
        return []


def get_oi(sym, earn_date):
    try:
        t    = yf.Ticker(sym)
        exps = t.options
        if not exps:
            return 0
        future = [e for e in exps if e >= earn_date]
        exp    = future[0] if future else exps[0]
        chain  = t.option_chain(exp)
        return int(chain.calls['openInterest'].fillna(0).sum() +
                   chain.puts['openInterest'].fillna(0).sum())
    except Exception:
        return 0


def is_notable(cap, ests, oi):
    if SHOW_ALL:
        return True
    return oi >= OI_MIN or (cap >= CAP_MIN and ests >= EST_MIN)


# --- Per-week run ---

def already_ran(year, week):
    conn = get_db()
    row  = conn.execute('SELECT 1 FROM earnings WHERE year=? AND week=? LIMIT 1', (year, week)).fetchone()
    conn.close()
    return row is not None


def run_week(monday):
    t0   = time.time()
    days = week_trading_days(monday)
    if not days:
        return

    iso_year, iso_week, _ = days[0].isocalendar()

    if already_ran(iso_year, iso_week):
        log.info(f'Earnings W{iso_week:02d}: already fetched, skipping')
        return

    all_rows = {}
    for day in days:
        for row in fetch_nasdaq(day):
            sym = row['symbol']
            if sym not in all_rows:
                all_rows[sym] = {**row, 'day': str(day), '_cap': parse_cap(row.get('marketCap', ''))}

    results = []
    for sym, row in all_rows.items():
        cap  = row['_cap']
        raw  = str(row.get('noOfEsts') or '0')
        ests = int(raw) if raw.isdigit() else 0
        oi   = get_oi(sym, row['day'])

        if not is_notable(cap, ests, oi):
            continue

        results.append({
            'sym':  sym,
            'name': row['name'][:28],
            'day':  row['day'],
            'time': fmt_time(row.get('time', '')),
            'eps':  row.get('epsForecast') or '-',
            'cap':  cap,
            'ests': ests,
            'oi':   oi,
        })

    results.sort(key=lambda r: r['oi'], reverse=True)

    # terminal table
    filt = 'all' if SHOW_ALL else f'OI ≥ {OI_MIN:,} or cap ≥ $10B + ≥{EST_MIN} ests'
    print(f"\n{'='*83}")
    print(f"  EARNINGS — {monday.strftime('%b %d')} – {(monday + timedelta(days=4)).strftime('%b %d, %Y')}  "
          f"(W{iso_week:02d})  |  {filt}")
    print(f"{'='*83}")

    by_day = {}
    for r in results:
        by_day.setdefault(r['day'], []).append(r)

    for day in sorted(by_day):
        d = date.fromisoformat(day)
        print(f"\n  {d.strftime('%A %b %d').upper()}")
        print(f"  {'-'*73}")
        print(f"  {'':4} {'Symbol':<8} {'OI':>10}  {'Cap':>7}  {'EPS Est':>8}  Company")
        print(f"  {'-'*73}")
        for r in by_day[day]:
            print(f"  {r['time']:<4} {r['sym']:<8} {r['oi']:>10,}  {fmt_cap(r['cap']):>7}  {r['eps']:>8}  {r['name']}")
        print(f"  {'-'*73}")

    save_earnings(results, iso_year, iso_week)
    log.info(f'Earnings W{iso_week:02d}: {len(results)} rows saved ({time.time()-t0:.1f}s)')

    lines = [f"EARNINGS W{iso_week:02d}  {monday.strftime('%b %d')}–{(monday+timedelta(days=4)).strftime('%b %d, %Y')}"]
    for day in sorted(by_day):
        d = date.fromisoformat(day)
        lines.append(f"\n{d.strftime('%a %b %d').upper()}")
        for r in by_day[day]:
            lines.append(f"{r['time']}  {r['sym']:<7} OI:{r['oi']:>8,}  {fmt_cap(r['cap']):>7}  EPS:{r['eps']:>6}  {r['name']}")
    send_telegram('\n'.join(lines))

    return results


# --- Entry point ---

def start_monday():
    if WEEK_NUM is not None:
        return date.fromisocalendar(date.today().year, WEEK_NUM, 1)
    offset = 1 if NEXT_WEEK else 0
    today  = date.today() + timedelta(weeks=offset)
    return today - timedelta(days=today.weekday())


monday = start_monday()

with log_run(log, 'earnings'):
    for i in range(WEEKS_N):
        run_week(monday + timedelta(weeks=i))

