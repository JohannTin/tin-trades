#!/usr/bin/env python3
"""
Macro events calendar — high impact USD only.

Input:  ForexFactory JSON feed (nfs.faireconomy.media)
Output: data/events.db (SQLite); Telegram message; terminal table

Usage:
    .venv/bin/python events.py          # this week
    .venv/bin/python events.py --next   # next week (used by Sunday cron)
"""
import sys
import os
import sqlite3
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
from utils import setup_logger, log_run, send_telegram

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

FF_THIS   = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
FF_NEXT   = 'https://nfs.faireconomy.media/ff_calendar_nextweek.json'
HEADERS   = {'User-Agent': 'Mozilla/5.0'}
EVENTS_DB = 'data/events.db'
ET        = ZoneInfo('America/New_York')

NEXT_WEEK = '--next' in sys.argv
log       = setup_logger('events', prefix='events')


def get_db():
    # Open (or create) events SQLite DB with WAL mode; creates table on first run.
    os.makedirs('data', exist_ok=True)
    conn = sqlite3.connect(EVENTS_DB)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            year       INTEGER NOT NULL,
            week       INTEGER NOT NULL,
            date       TEXT NOT NULL,
            time       TEXT,
            title      TEXT NOT NULL,
            impact     TEXT,
            forecast   TEXT,
            previous   TEXT,
            actual     TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE (year, week, date, title)
        )
    ''')
    conn.commit()
    return conn


def save_events(rows, year, week):
    # Upsert ForexFactory event rows keyed by (year, week, date, title).
    conn       = get_db()
    fetched_at = datetime.now().isoformat(timespec='seconds')
    conn.executemany(
        '''INSERT OR REPLACE INTO events
           (year, week, date, time, title, impact, forecast, previous, actual, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        [(year, week, r['date'], r['time'], r['title'],
          r['impact'], r['forecast'], r['previous'], r['actual'], fetched_at)
         for r in rows]
    )
    conn.commit()
    conn.close()


def fetch_ff():
    # GET ForexFactory this-week or next-week JSON; returns [] on any error.
    url = FF_NEXT if NEXT_WEEK else FF_THIS
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f'Failed to fetch ForexFactory: {e}')
        return []


def to_et(date_str):
    # Parse an ISO datetime string and convert to Eastern Time.
    return datetime.fromisoformat(date_str).astimezone(ET)


def already_ran(year, week):
    # Check if any events rows already exist for year/week; used to skip duplicate runs.
    conn = get_db()
    row  = conn.execute('SELECT 1 FROM events WHERE year=? AND week=? LIMIT 1', (year, week)).fetchone()
    conn.close()
    return row is not None


def run():
    # Full pipeline: fetch ForexFactory, filter to high-impact USD, save to DB, print table, send Telegram.
    t0 = time.time()

    today = datetime.now(ET)
    offset = 1 if NEXT_WEEK else 0
    check = today + timedelta(weeks=offset)
    iso_year, iso_week, _ = check.isocalendar()
    if already_ran(iso_year, iso_week):
        log.info(f'Events W{iso_week:02d}: already fetched, skipping')
        return

    raw      = fetch_ff()
    filtered = [e for e in raw if e.get('country') == 'USD' and e.get('impact') == 'High']

    if not filtered:
        label = 'NEXT WEEK' if NEXT_WEEK else 'THIS WEEK'
        log.info(f'No high-impact USD events found for {label}.')
        return

    by_day = {}
    for e in filtered:
        et  = to_et(e['date'])
        day = et.strftime('%Y-%m-%d')
        by_day.setdefault(day, []).append((et, e))

    first_et              = to_et(filtered[0]['date'])
    iso_year, iso_week, _ = first_et.isocalendar()
    label = 'NEXT WEEK' if NEXT_WEEK else 'THIS WEEK'

    # terminal table
    print(f"\n{'='*72}")
    print(f"  MACRO EVENTS — {label}  (W{iso_week:02d})  |  High impact USD only")
    print(f"{'='*72}")

    db_rows = []
    for day in sorted(by_day):
        d = datetime.strptime(day, '%Y-%m-%d')
        print(f"\n  {d.strftime('%A %b %d').upper()}")
        print(f"  {'-'*70}")
        print(f"  {'Time ET':<10} {'Event':<38} {'Forecast':>9}  {'Previous':>9}")
        print(f"  {'-'*70}")
        for et, e in sorted(by_day[day], key=lambda x: x[0]):
            time_str = et.strftime('%H:%M')
            forecast = e.get('forecast') or '-'
            previous = e.get('previous') or '-'
            print(f"  {time_str:<10} {e['title']:<38} {forecast:>9}  {previous:>9}")
            db_rows.append({
                'date':     day,
                'time':     time_str,
                'title':    e['title'],
                'impact':   e['impact'],
                'forecast': e.get('forecast') or None,
                'previous': e.get('previous') or None,
                'actual':   e.get('actual')   or None,
            })
        print(f"  {'-'*70}")

    save_events(db_rows, iso_year, iso_week)
    log.info(f'Events W{iso_week:02d}: {len(db_rows)} rows saved ({time.time()-t0:.1f}s)')

    lines = [f"MACRO EVENTS W{iso_week:02d}  {label}  — High impact USD"]
    for day in sorted(by_day):
        d = datetime.strptime(day, '%Y-%m-%d')
        lines.append(f"\n{d.strftime('%a %b %d').upper()}")
        for et, e in sorted(by_day[day], key=lambda x: x[0]):
            fc = e.get('forecast') or '-'
            pr = e.get('previous') or '-'
            lines.append(f"{et.strftime('%H:%M')}  {e['title']:<36} fc:{fc:>7}  pr:{pr:>7}")
    send_telegram('\n'.join(lines))


with log_run(log, 'events'):
    run()
