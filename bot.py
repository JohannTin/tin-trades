#!/usr/bin/env python3
"""
Telegram command bot.

Commands:
  /earnings          — this week
  /earnings today    — today only
  /earnings next     — next week
  /events            — this week
  /events next       — next week

Run: set -a && source .env && set +a && .venv/bin/python bot.py
"""
import os, sqlite3, time, requests
from datetime import date, timedelta
from utils import setup_logger

TOKEN   = os.environ['TELEGRAM_TOKEN']
BASE    = f'https://api.telegram.org/bot{TOKEN}'
HEADERS = {'User-Agent': 'Mozilla/5.0'}
log     = setup_logger('bot', prefix='bot')


def send(chat_id, text):
    requests.post(f'{BASE}/sendMessage',
                  json={'chat_id': chat_id, 'text': f'<pre>{text}</pre>', 'parse_mode': 'HTML'},
                  timeout=10)


def fmt_cap(n):
    if not n: return '-'
    if n >= 1_000_000_000_000: return f'${n/1e12:.1f}T'
    if n >= 1_000_000_000:     return f'${n/1e9:.1f}B'
    if n >= 1_000_000:         return f'${n/1e6:.0f}M'
    return '-'


def earnings_text(filter_date=None, next_week=False):
    today  = date.today()
    offset = 1 if next_week else 0
    monday = today + timedelta(weeks=offset)
    monday = monday - timedelta(days=monday.weekday())
    iso_year, iso_week, _ = monday.isocalendar()

    conn = sqlite3.connect('data/earnings.db')
    if filter_date:
        rows = conn.execute(
            'SELECT date, symbol, name, report_time, eps_est, market_cap, oi FROM earnings '
            'WHERE date=? ORDER BY oi DESC',
            (str(filter_date),)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT date, symbol, name, report_time, eps_est, market_cap, oi FROM earnings '
            'WHERE year=? AND week=? ORDER BY oi DESC',
            (iso_year, iso_week)
        ).fetchall()
    conn.close()

    if not rows:
        return 'No earnings found.'

    by_day = {}
    for day, sym, name, rtime, eps, cap, oi in rows:
        by_day.setdefault(day, []).append((sym, name, rtime, eps, cap or 0, oi or 0))

    label = f"W{iso_week:02d}  {monday.strftime('%b %d')}–{(monday+timedelta(days=4)).strftime('%b %d')}"
    if filter_date:
        label = str(filter_date)
    lines = [f"EARNINGS  {label}"]
    for day in sorted(by_day):
        d = date.fromisoformat(day)
        lines.append(f"\n{d.strftime('%a %b %d').upper()}")
        for sym, name, rtime, eps, cap, oi in by_day[day]:
            lines.append(f"{rtime or ' - ':<4}  {sym:<7} OI:{oi:>8,}  {fmt_cap(cap):>7}  EPS:{eps or '-':>6}  {name or ''}")
    return '\n'.join(lines)


def events_text(next_week=False):
    url = ('https://nfs.faireconomy.media/ff_calendar_nextweek.json' if next_week
           else 'https://nfs.faireconomy.media/ff_calendar_thisweek.json')
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        ET  = ZoneInfo('America/New_York')
        raw = requests.get(url, headers=HEADERS, timeout=10).json()
    except Exception as e:
        return f'Failed to fetch events: {e}'

    filtered = [e for e in raw if e.get('country') == 'USD' and e.get('impact') == 'High']
    if not filtered:
        return 'No high-impact USD events found.'

    by_day = {}
    for e in filtered:
        et  = datetime.fromisoformat(e['date']).astimezone(ET)
        day = et.strftime('%Y-%m-%d')
        by_day.setdefault(day, []).append((et, e))

    first_et      = datetime.fromisoformat(filtered[0]['date']).astimezone(ET)
    _, iso_week, _ = first_et.isocalendar()
    label = 'NEXT WEEK' if next_week else 'THIS WEEK'

    lines = [f"MACRO EVENTS W{iso_week:02d}  {label}  — High impact USD"]
    for day in sorted(by_day):
        d = datetime.strptime(day, '%Y-%m-%d')
        lines.append(f"\n{d.strftime('%a %b %d').upper()}")
        for et, e in sorted(by_day[day], key=lambda x: x[0]):
            fc = e.get('forecast') or '-'
            pr = e.get('previous') or '-'
            lines.append(f"{et.strftime('%H:%M')}  {e['title']:<36} fc:{fc:>7}  pr:{pr:>7}")
    return '\n'.join(lines)


def handle(msg):
    chat_id = msg['chat']['id']
    text    = msg.get('text', '').strip()
    parts   = text.lower().split()
    cmd     = parts[0].lstrip('/').split('@')[0] if parts else ''
    args    = parts[1:]

    if cmd == 'earnings':
        if 'today' in args:
            reply = earnings_text(filter_date=date.today())
        elif 'next' in args:
            reply = earnings_text(next_week=True)
        else:
            reply = earnings_text()
    elif cmd == 'events':
        reply = events_text(next_week='next' in args)
    elif cmd == 'help' or cmd == 'start':
        reply = ('/earnings         — this week\n'
                 '/earnings today   — today only\n'
                 '/earnings next    — next week\n'
                 '/events           — this week\n'
                 '/events next      — next week')
    else:
        return

    log.info(f'cmd=/{cmd} args={args} chat={chat_id}')
    send(chat_id, reply)


def poll():
    offset = 0
    log.info('Bot started.')
    while True:
        try:
            r = requests.get(f'{BASE}/getUpdates',
                             params={'offset': offset, 'timeout': 30},
                             timeout=35)
            for upd in r.json().get('result', []):
                offset = upd['update_id'] + 1
                if 'message' in upd:
                    handle(upd['message'])
        except Exception as e:
            log.error(f'Poll error: {e}')
            time.sleep(5)


if __name__ == '__main__':
    poll()
