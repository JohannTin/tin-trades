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
import json, math, os, sqlite3, threading, time, warnings, requests
from datetime import date, timedelta, datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from utils import setup_logger

ET = ZoneInfo('America/New_York')

warnings.filterwarnings('ignore')

_alerts = {}  # ticker -> {chat_id, wall, first_pos}

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


def _bs_gamma(S, K, T, sigma, r=0.05):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1**2) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))


def gex_live(ticker):
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return 'yfinance/pandas not installed.'

    tk   = yf.Ticker(ticker.upper())
    spot = tk.fast_info.get('lastPrice') or tk.fast_info.get('previousClose')
    if not spot:
        return f'Could not get spot price for {ticker.upper()}.'

    exp  = tk.options[0]
    T    = max((date.fromisoformat(exp) - date.today()).days / 365, 1 / 365)

    chain = tk.option_chain(exp)
    rows  = []
    for sign, df in [(1, chain.calls), (-1, chain.puts)]:
        for _, r in df.iterrows():
            iv  = r.get('impliedVolatility', 0) or 0
            oi  = r.get('openInterest', 0) or 0
            g   = _bs_gamma(spot, r['strike'], T, iv)
            rows.append({'strike': r['strike'], 'gex': sign * oi * g * 100 * spot})

    agg = (pd.DataFrame(rows).groupby('strike')['gex'].sum()
             .reset_index().sort_values('strike'))
    agg['cum_gex'] = agg['gex'].cumsum()

    total = agg['gex'].sum()
    wall  = float(agg.loc[agg['gex'].abs().idxmax(), 'strike'])

    shifted   = agg['cum_gex'].shift(1, fill_value=agg['cum_gex'].iloc[0])
    flip_rows = agg[shifted * agg['cum_gex'] < 0]
    flip      = float(flip_rows['strike'].iloc[0]) if not flip_rows.empty else None

    pos_above  = agg[(agg['gex'] > 0) & (agg['strike'] > spot)]
    first_pos  = float(pos_above['strike'].min()) if not pos_above.empty else None
    call_wall  = float(pos_above.loc[pos_above['gex'].idxmax(), 'strike']) if not pos_above.empty else None

    sign_str  = 'neg (AMP)' if total < 0 else 'pos (PIN)'
    flip_str  = f'{flip:.2f}' if flip else 'none in range'
    first_str = f'{first_pos:.2f}' if first_pos else 'none'
    cwall_str = f'{call_wall:.2f}' if call_wall else 'none'

    return (
        f'GAMMA  {ticker.upper()}  {exp}\n'
        f'Spot   {spot:.2f}\n'
        f'Wall   {wall:.2f}\n'
        f'Flip   {flip_str}\n'
        f'+GEX   {first_str}  →  {cwall_str}\n'
        f'Total  {total/1e6:+.1f}M  {sign_str}'
    )


def alert_set(ticker, chat_id):
    import yfinance as yf
    import pandas as pd

    ticker = ticker.upper()
    tk     = yf.Ticker(ticker)
    spot   = tk.fast_info.get('lastPrice') or tk.fast_info.get('previousClose')
    if not spot:
        return f'Could not get spot for {ticker}.'

    exp = tk.options[0]
    T   = max((date.fromisoformat(exp) - date.today()).days / 365, 1 / 365)

    rows = []
    chain = tk.option_chain(exp)
    for sign, df in [(1, chain.calls), (-1, chain.puts)]:
        for _, r in df.iterrows():
            iv = r.get('impliedVolatility', 0) or 0
            oi = r.get('openInterest', 0) or 0
            g  = _bs_gamma(spot, r['strike'], T, iv)
            rows.append({'strike': r['strike'], 'gex': sign * oi * g * 100 * spot})

    agg      = pd.DataFrame(rows).groupby('strike')['gex'].sum().reset_index().sort_values('strike')
    wall     = float(agg.loc[agg['gex'].abs().idxmax(), 'strike'])
    pos_above = agg[(agg['gex'] > 0) & (agg['strike'] > spot)]
    first_pos = float(pos_above['strike'].min()) if not pos_above.empty else None

    _alerts[ticker] = {'chat_id': chat_id, 'wall': wall, 'first_pos': first_pos}
    log.info(f'alert set: {ticker} wall={wall} first_pos={first_pos} chat={chat_id}')

    pos_str = f'{first_pos:.2f}' if first_pos else 'none'
    return (f'Alert set for {ticker}\n'
            f'Spot      {spot:.2f}\n'
            f'Wall      {wall:.2f}  ← fires if spot < this\n'
            f'+GEX zone {pos_str}  ← fires if spot >= this\n'
            f'Clears at 4:00 PM ET')


def alert_thread():
    import yfinance as yf

    while True:
        time.sleep(300)
        now = datetime.now(ET)

        if now.hour >= 16:
            if _alerts:
                for ticker, a in list(_alerts.items()):
                    send(a['chat_id'], f'Market closed. Alert cleared for {ticker}.')
                _alerts.clear()
                log.info('alerts cleared at EOD')
            continue

        for ticker, a in list(_alerts.items()):
            try:
                spot = yf.Ticker(ticker).fast_info.get('lastPrice')
                if not spot:
                    continue
                if spot < a['wall']:
                    msg = f'ALERT  {ticker}\nBroke below wall {a["wall"]:.2f}\nSpot {spot:.2f}'
                    send(a['chat_id'], msg)
                    log.info(f'alert fired (wall break): {ticker} spot={spot}')
                    del _alerts[ticker]
                elif a['first_pos'] and spot >= a['first_pos']:
                    msg = f'ALERT  {ticker}\nEntered +GEX zone {a["first_pos"]:.2f}\nSpot {spot:.2f}'
                    send(a['chat_id'], msg)
                    log.info(f'alert fired (+GEX): {ticker} spot={spot}')
                    del _alerts[ticker]
            except Exception as e:
                log.error(f'alert check error {ticker}: {e}')


def gamma_text(ticker='SPY'):
    p = Path(f'data/gamma/{ticker}_summary.json')
    if not p.exists():
        return f'No GEX data for {ticker} yet. Runs at 9:05 AM ET.'
    s = json.loads(p.read_text())
    if s['date'] != date.today().isoformat():
        return f"GEX data is from {s['date']}, not today."
    sign     = 'neg (vol AMP)' if s['total_gex'] < 0 else 'pos (vol PIN)'
    flip_str = f"{s['flip']:.2f}" if s['flip'] else 'N/A'
    return (
        f"GAMMA  {ticker}  {s['date']}\n"
        f"Spot  {s['spot']:.2f}\n"
        f"Flip  {flip_str}\n"
        f"Wall  {s['wall']:.2f}\n"
        f"GEX   {s['total_gex']:+.2f}B  {sign}"
    )


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

    if cmd == 'gamma':
        if not args:
            reply = gamma_text()
        elif Path(f'data/gamma/{args[0].upper()}_summary.json').exists():
            reply = gamma_text(args[0].upper())
        else:
            reply = gex_live(args[0])
    elif cmd == 'alert':
        if not args or args[0] == 'off':
            _alerts.clear()
            reply = 'All alerts cleared.'
        else:
            reply = alert_set(args[0], chat_id)
    elif cmd == 'earnings':
        if 'today' in args:
            reply = earnings_text(filter_date=date.today())
        elif 'next' in args:
            reply = earnings_text(next_week=True)
        else:
            reply = earnings_text()
    elif cmd == 'events':
        reply = events_text(next_week='next' in args)
    elif cmd == 'help' or cmd == 'start':
        reply = ('/gamma            — SPY gamma exposure\n'
                 '/gamma TICKER     — live GEX for any ticker\n'
                 '/alert TICKER     — alert when wall breaks or +GEX hit\n'
                 '/alert off        — clear all alerts\n'
                 '/earnings         — this week\n'
                 '/earnings today   — today only\n'
                 '/earnings next    — next week\n'
                 '/events           — this week\n'
                 '/events next      — next week')
    else:
        return

    log.info(f'cmd=/{cmd} args={args} chat={chat_id}')
    send(chat_id, reply)


def poll():
    threading.Thread(target=alert_thread, daemon=True).start()
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
