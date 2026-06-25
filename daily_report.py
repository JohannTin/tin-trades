#!/usr/bin/env python3
"""
Daily pre-market brief: compute GEX for 10 tickers, push DAILY.md + daily.html.

Usage:
    .venv/bin/python daily_report.py           # skip if market closed
    .venv/bin/python daily_report.py --force   # run regardless
"""
import json
import math
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

import gex
from utils import setup_logger, is_market_open, log_run

BASE  = Path(__file__).parent
TODAY = date.today().isoformat()
YEAR  = date.today().year
FORCE = '--force' in sys.argv

with open(BASE / 'config.yaml') as f:
    cfg = yaml.safe_load(f)

TICKERS = cfg['watchlist'] + cfg.get('mag7', [])
log = setup_logger('daily', prefix='daily')


# ── GEX computation ──────────────────────────────────────────────────────────

DB_PATH = BASE / 'data/gamma/gex_levels.db'


def ticker_summary(ticker):
    d    = dict(yf.Ticker(ticker).fast_info)
    spot = float(d.get('lastPrice') or d.get('previousClose') or 0)

    # Prefer 9:15 AM IBKR snapshot from intraday table
    snaps = gex.load_snapshots(DB_PATH, 'intraday', TODAY, ticker)
    first = snaps[0] if snaps else None
    if first:
        wall_0 = first['wall_0']; supp_0 = first['support_0']; res_0 = first['resistance_0']
        net_0  = (first['net_0'] or 0) * 1e9
        wall_w = first['wall_w']; supp_w = first['support_w']; res_w = first['resistance_w']
        net_w  = (first['net_w'] or 0) * 1e9
    else:
        # Fallback: BS from chain parquet
        chain = pd.DataFrame()
        oi = gex.load_oi(ticker)
        if oi is not None:
            chain = oi

        gex_0 = gex.chain_to_gex(chain[chain['expiry'] == TODAY], spot) if not chain.empty else pd.DataFrame(columns=['strike','expiry','gex'])

        # Weekly: prefer IBKR parquet
        gfile   = BASE / f'data/gamma/{ticker}_gex_{YEAR}.parquet'
        gex_w   = pd.DataFrame(columns=['strike', 'expiry', 'gex'])
        if gfile.exists():
            _g = pd.read_parquet(gfile)
            _today_g = _g[_g['date'] == TODAY][['strike', 'expiry', 'gex']]
            if not _today_g.empty:
                gex_w = _today_g.copy()
        if gex_w.empty:
            gex_w = gex.chain_to_gex(chain, spot)

        wall_0, supp_0, res_0, net_0 = gex.levels(gex_0, spot)
        wall_w, supp_w, res_w, net_w = gex.levels(gex_w, spot)

    if spot == 0:
        return {'ticker': ticker, 'spot': 0,
                'wall_0': None, 'support_0': None, 'resistance_0': None, 'net_0': 0,
                'wall_w': None, 'support_w': None, 'resistance_w': None, 'net_w': 0,
                'env': 'NEG', 'strikes': []}

    # Tile bars: 0DTE GEX (falls back to weekly if 0DTE empty)
    primary = gex_0 if not gex_0.empty else gex_w
    agg_t   = primary.groupby('strike')['gex'].sum().reset_index()
    all_s   = sorted(agg_t['strike'].unique())
    ci      = min(range(len(all_s)), key=lambda i: abs(all_s[i] - spot))
    lo, hi  = max(0, ci - 8), min(len(all_s), ci + 9)
    tile    = agg_t[agg_t['strike'].isin(all_s[lo:hi])].sort_values('strike', ascending=False)

    return {
        'ticker':       ticker,
        'spot':         round(spot, 2),
        'wall_0':       wall_0,
        'support_0':    supp_0,
        'resistance_0': res_0,
        'net_0':        round(net_0),
        'wall_w':       wall_w,
        'support_w':    supp_w,
        'resistance_w': res_w,
        'net_w':        round(net_w),
        'env':          'NEG' if net_0 < 0 else 'POS',
        'strikes':      tile[['strike', 'gex']].to_dict('records'),
    }


# ── Data sources ──────────────────────────────────────────────────────────────

def read_earnings():
    db = BASE / 'data/earnings.db'
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT symbol, name, report_time, eps_est, market_cap FROM earnings WHERE date=? ORDER BY oi DESC',
        (TODAY,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def read_events():
    db = BASE / 'data/events.db'
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT time, title, forecast, previous FROM events WHERE date=? ORDER BY time',
        (TODAY,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]



# ── SVG tile renderer (static, no JS) ────────────────────────────────────────

def fmt_cap(n):
    if not n: return '—'
    if n >= 1e12: return f'{n/1e12:.1f}T'
    if n >= 1e9:  return f'{n/1e9:.1f}B'
    return f'{n/1e6:.0f}M'


def render_tile_svg(d):
    strikes = d['strikes']
    W = 240
    if not strikes:
        H = 80
        return (
            f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{W}" height="{H}" fill="#161616"/>'
            f'<text x="8" y="20" fill="#d4d4d4" font-size="13" font-weight="bold" font-family="monospace">{d["ticker"]}</text>'
            f'<text x="8" y="40" fill="#444" font-size="10" font-family="monospace">no data</text>'
            f'</svg>'
        )

    maxAbs   = max(abs(s['gex']) for s in strikes) or 1
    isNeg    = d['net_0'] < 0
    envColor = '#00e5e5' if isNeg else '#e040fb'
    ROW_H, HDR_H, FOOT_H = 20, 30, 58
    LABEL_W, BAR_X, BAR_MAX = 40, 44, 148
    H = HDR_H + len(strikes) * ROW_H + FOOT_H

    wall_0 = d.get('wall_0');  supp_0 = d.get('support_0');  res_0 = d.get('resistance_0')
    wall_w = d.get('wall_w');  supp_w = d.get('support_w');  res_w = d.get('resistance_w')
    spotStrike = min(strikes, key=lambda s: abs(s['strike'] - d['spot']))['strike']

    p = []
    p.append(f'<rect width="{W}" height="{H}" fill="#161616"/>')
    p.append(f'<rect width="{W}" height="{HDR_H}" fill="#1c1c1c"/>')
    p.append(f'<text x="8" y="19" fill="#d4d4d4" font-size="13" font-weight="bold" font-family="monospace">{d["ticker"]}</text>')
    p.append(f'<text x="{W-8}" y="13" text-anchor="end" fill="#888" font-size="10" font-family="monospace">{d["spot"]}</text>')
    env_sym = '●' if isNeg else '◆'
    p.append(f'<text x="{W-8}" y="26" text-anchor="end" fill="{envColor}" font-size="9" font-family="monospace">{"NEG" if isNeg else "POS"} {env_sym}</text>')

    for i, row in enumerate(strikes):
        y       = HDR_H + i * ROW_H
        strike  = row['strike']
        isWall0 = strike == wall_0
        isWallW = strike == wall_w and strike != wall_0
        isSpot  = strike == spotStrike
        isSupp0 = strike == supp_0
        isRes0  = strike == res_0
        isSuppW = strike == supp_w and strike != supp_0
        isResW  = strike == res_w  and strike != res_0

        rowBg = '#1e1a0e' if isSpot else ('#181818' if i % 2 else '#161616')
        p.append(f'<rect x="0" y="{y}" width="{W}" height="{ROW_H}" fill="{rowBg}"/>')
        if isWall0:
            p.append(f'<rect x="{LABEL_W}" y="{y}" width="{W-LABEL_W}" height="{ROW_H}" fill="rgba(255,215,0,0.08)"/>')
        elif isWallW:
            p.append(f'<rect x="{LABEL_W}" y="{y}" width="{W-LABEL_W}" height="{ROW_H}" fill="rgba(255,215,0,0.03)"/>')

        sc = '#ffd700' if isWall0 else ('#b8860b' if isWallW else ('#fbbf24' if isSpot else '#555'))
        p.append(f'<text x="{LABEL_W-3}" y="{y+ROW_H//2+4}" text-anchor="end" fill="{sc}" font-size="10" font-family="monospace">{int(strike)}</text>')

        bw = math.sqrt(abs(row['gex']) / maxAbs) * BAR_MAX
        bc = '#ffd700' if isWall0 else ('#e040fb' if row['gex'] >= 0 else '#00e5e5')
        if bw > 0.5:
            p.append(f'<rect x="{BAR_X}" y="{y+4}" width="{bw:.1f}" height="{ROW_H-8}" fill="{bc}" opacity="0.85" rx="1"/>')

        # Right-side labels: 0DTE markers solid, weekly markers dimmer
        lbl, lcol = '', ''
        if isWall0:   lbl, lcol = '&#9733;', '#ffd700'
        elif isRes0:  lbl, lcol = '&#9650; R', '#e040fb'
        elif isSupp0: lbl, lcol = '&#9660; S', '#00e5e5'
        elif isWallW: lbl, lcol = '&#9670;W', '#7a6000'
        elif isResW:  lbl, lcol = '&#9651;W', '#7a2060'
        elif isSuppW: lbl, lcol = '&#9661;W', '#006060'
        if lbl:
            p.append(f'<text x="{W-4}" y="{y+ROW_H//2+4}" text-anchor="end" fill="{lcol}" font-size="9" font-family="monospace">{lbl}</text>')

        if isSpot:
            p.append(f'<line x1="{BAR_X}" y1="{y+ROW_H//2}" x2="{BAR_X+BAR_MAX}" y2="{y+ROW_H//2}" stroke="#fbbf24" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.7"/>')

    fy = HDR_H + len(strikes) * ROW_H
    p.append(f'<rect x="0" y="{fy}" width="{W}" height="{FOOT_H}" fill="#1a1a1a"/>')
    p.append(f'<line x1="0" y1="{fy}" x2="{W}" y2="{fy}" stroke="#222" stroke-width="1"/>')

    def _fmt(v): return str(int(v)) if v is not None else '—'
    # 0DTE row
    p.append(f'<text x="8" y="{fy+13}" fill="#666" font-size="8" font-family="monospace">0D &#9733;{_fmt(wall_0)} &#9650;{_fmt(res_0)} &#9660;{_fmt(supp_0)}</text>')
    # Weekly row
    p.append(f'<text x="8" y="{fy+25}" fill="#444" font-size="8" font-family="monospace"> W &#9670;{_fmt(wall_w)} &#9651;{_fmt(res_w)} &#9661;{_fmt(supp_w)}</text>')

    net0K  = round(d['net_0'] / 1000)
    net0Str = f'+${abs(net0K):,}K' if net0K >= 0 else f'-${abs(net0K):,}K'
    p.append(f'<text x="8" y="{fy+43}" fill="{envColor}" font-size="11" font-weight="bold" font-family="monospace">0D {net0Str}</text>')
    netWK   = round(d['net_w'] / 1000)
    netWStr = f'+${abs(netWK):,}K' if netWK >= 0 else f'-${abs(netWK):,}K'
    wEnvC   = '#00e5e5' if d['net_w'] < 0 else '#e040fb'
    p.append(f'<text x="{W//2+4}" y="{fy+43}" fill="{wEnvC}" font-size="9" font-family="monospace" opacity="0.6">W {netWStr}</text>')

    inner = ''.join(p)
    return f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">{inner}</svg>'


# ── Output files ──────────────────────────────────────────────────────────────

def write_daily_md(summaries, earnings, events):
    lines = [f'# Daily Overview — {TODAY}', '']

    lines += ['## Earnings Today', '']
    if earnings:
        lines.append('| Symbol | Name | Time | EPS Est | Cap |')
        lines.append('|--------|------|------|---------|-----|')
        for r in earnings:
            lines.append(f"| {r['symbol']} | {r['name'] or ''} | {r['report_time'] or ''} | {r['eps_est'] or ''} | {fmt_cap(r['market_cap'])} |")
    else:
        lines.append('_No earnings today_')
    lines.append('')

    lines += ['## Macro Events', '']
    if events:
        lines.append('| Time (ET) | Event | Forecast | Previous |')
        lines.append('|-----------|-------|----------|----------|')
        for r in events:
            lines.append(f"| {r['time'] or ''} | {r['title']} | {r['forecast'] or ''} | {r['previous'] or ''} |")
    else:
        lines.append('_No events today_')
    lines.append('')

    lines += ['## GEX Summary', '']
    lines.append('| Ticker | Spot | 0DTE Wall | 0DTE Supp | 0DTE Res | Net 0DTE | Wk Wall | Wk Supp | Wk Res | Net Wk | Env |')
    lines.append('|--------|------|-----------|-----------|----------|----------|---------|---------|--------|--------|-----|')
    for s in summaries:
        def _f(v): return str(int(v)) if v is not None else '—'
        net0 = round(s['net_0'] / 1000)
        netw = round(s['net_w'] / 1000)
        lines.append(
            f"| {s['ticker']} | {s['spot']} "
            f"| {_f(s['wall_0'])} | {_f(s['support_0'])} | {_f(s['resistance_0'])} | ${net0:+,}K "
            f"| {_f(s['wall_w'])} | {_f(s['support_w'])} | {_f(s['resistance_w'])} | ${netw:+,}K "
            f"| {s['env']} |"
        )

    (BASE / 'DAILY.md').write_text('\n'.join(lines) + '\n')


def write_gex_html(summaries, earnings, events):
    earning_rows = ''.join(
        f'<tr><td><strong>{r["symbol"]}</strong></td>'
        f'<td style="text-align:left">{r["name"] or ""}</td>'
        f'<td class="dim">{r["report_time"] or ""}</td>'
        f'<td>{r["eps_est"] or ""}</td>'
        f'<td class="dim">{fmt_cap(r["market_cap"])}</td></tr>'
        for r in earnings
    ) or '<tr><td colspan="5" class="empty">No earnings today</td></tr>'

    event_rows = ''.join(
        f'<tr><td class="dim">{r["time"] or ""}</td>'
        f'<td style="text-align:left">{r["title"]}</td>'
        f'<td class="dim">{r["forecast"] or ""}</td>'
        f'<td class="dim">{r["previous"] or ""}</td></tr>'
        for r in events
    ) or '<tr><td colspan="4" class="empty">No events today</td></tr>'

    tiles = ''.join(f'<div class="tile">{render_tile_svg(s)}</div>' for s in summaries)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tin-trades &mdash; {TODAY}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: monospace; background: #0e0e0e; color: #d4d4d4; padding: 20px; }}
h1 {{ font-size: 12px; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 20px; }}
h2 {{ font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }}
.top {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }}
.panel {{ background: #161616; border: 1px solid #222; border-radius: 4px; padding: 14px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
th {{ color: #444; padding: 3px 6px 6px; border-bottom: 1px solid #222; text-align: right; }}
th:first-child {{ text-align: left; }}
td {{ padding: 4px 6px; border-bottom: 1px solid #1a1a1a; color: #ccc; text-align: right; vertical-align: middle; }}
td:first-child {{ text-align: left; }}
tr:last-child td {{ border-bottom: none; }}
.dim {{ color: #666; }}
.empty {{ color: #444; text-align: left !important; }}
.gex-wrap {{ margin-bottom: 8px; }}
.gex-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }}
.tile {{ border-radius: 3px; overflow: hidden; }}
.tile svg {{ display: block; width: 100%; height: auto; }}
</style>
</head>
<body>
<h1>tin-trades &mdash; {TODAY}</h1>

<div class="top">
  <div class="panel">
    <h2>Earnings Today</h2>
    <table>
      <tr><th>Symbol</th><th style="text-align:left">Name</th><th>Time</th><th>EPS Est</th><th>Cap</th></tr>
      {earning_rows}
    </table>
  </div>
  <div class="panel">
    <h2>Events Today</h2>
    <table>
      <tr><th style="text-align:left">Time</th><th style="text-align:left">Event</th><th>Forecast</th><th>Prev</th></tr>
      {event_rows}
    </table>
  </div>
</div>

<div class="gex-wrap">
  <h2>Gamma Exposure</h2>
  <div class="gex-grid">
    {tiles}
  </div>
</div>
</body>
</html>"""

    (BASE / 'daily.html').write_text(html)


def save_daily_summary(summaries):
    out = BASE / 'data/gamma/daily_summary.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({'date': TODAY, 'tickers': summaries}, default=str))


def save_morning_levels(summaries):
    snap_time = datetime.now().strftime('%H:%M')
    for s in summaries:
        gex.save_snapshot(
            DB_PATH, 'morning', TODAY, s['ticker'], snap_time, s['spot'],
            (s['wall_0'], s['support_0'], s['resistance_0'], s['net_0']),
            (s['wall_w'], s['support_w'], s['resistance_w'], s['net_w']),
        )


def git_push():
    subprocess.run(['git', 'add', 'DAILY.md', 'daily.html'], check=True, cwd=BASE)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=BASE).returncode != 0:
        subprocess.run(['git', 'commit', '-m', f'daily: {TODAY}'], check=True, cwd=BASE)
    subprocess.run(['git', 'push'], check=True, cwd=BASE)


def main():
    with log_run(log, 'daily'):
        if not is_market_open() and not FORCE:
            log.info('Market closed today. Skipping. (--force to override)')
            return
        log.info(f'Computing GEX for {len(TICKERS)} tickers: {", ".join(TICKERS)}')
        summaries = [ticker_summary(t) for t in TICKERS]
        save_daily_summary(summaries)
        save_morning_levels(summaries)
        earnings = read_earnings()
        events   = read_events()
        write_daily_md(summaries, earnings, events)
        write_gex_html(summaries, earnings, events)
        log.info('Pushing to GitHub...')
        git_push()


if __name__ == '__main__':
    main()
