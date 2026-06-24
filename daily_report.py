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
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

from utils import setup_logger, is_market_open, log_run

BASE  = Path(__file__).parent
TODAY = date.today().isoformat()
YEAR  = date.today().year
R     = 0.05  # risk-free rate for Black-Scholes
FORCE = '--force' in sys.argv

with open(BASE / 'config.yaml') as f:
    cfg = yaml.safe_load(f)

TICKERS = cfg['watchlist'] + cfg.get('mag7', [])
log = setup_logger('daily', prefix='daily')


# ── GEX computation ──────────────────────────────────────────────────────────

def bs_gamma(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 ** 2) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))


def compute_gex(ticker, spot):
    pfile = BASE / f'data/options/{ticker}_chain_{YEAR}.parquet'
    if not pfile.exists():
        return pd.DataFrame(columns=['strike', 'expiry', 'gex'])
    df = pd.read_parquet(pfile)
    df = df[df['date'] == TODAY].copy()
    if df.empty:
        return pd.DataFrame(columns=['strike', 'expiry', 'gex'])
    rows = []
    for _, row in df.iterrows():
        T = (pd.to_datetime(row['expiry']) - pd.Timestamp.today()).days / 365
        if T <= 0:
            continue
        cg  = bs_gamma(spot, row['strike'], T, float(row.get('call_iv') or 0))
        pg  = bs_gamma(spot, row['strike'], T, float(row.get('put_iv') or 0))
        net = (cg * float(row.get('call_oi') or 0) - pg * float(row.get('put_oi') or 0)) * 100 * spot ** 2
        rows.append({'strike': float(row['strike']), 'expiry': str(row['expiry']), 'gex': net})
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['strike', 'expiry', 'gex'])


def ticker_summary(ticker):
    info = yf.Ticker(ticker).fast_info
    spot = float(info.get('last_price') or info.get('previous_close') or 0)

    gex_df = compute_gex(ticker, spot)

    # Persist to parquet for server.py /api/gamma?ticker=
    out = BASE / f'data/gamma/{ticker}_gex_{YEAR}.parquet'
    out.parent.mkdir(exist_ok=True)
    if not gex_df.empty:
        save_df = gex_df.assign(date=TODAY)
        if out.exists():
            existing = pd.read_parquet(out)
            save_df = pd.concat([existing[existing['date'] != TODAY], save_df], ignore_index=True)
        save_df.to_parquet(out, index=False)

    if gex_df.empty or spot == 0:
        return {'ticker': ticker, 'spot': spot, 'wall': None, 'support': None,
                'resistance': None, 'net': 0, 'env': 'NEG', 'strikes': []}

    agg  = gex_df.groupby('strike')['gex'].sum().reset_index()
    wall = float(agg.loc[agg['gex'].abs().idxmax(), 'strike'])
    net  = float(agg['gex'].sum())

    below = agg[agg['strike'] <= spot].sort_values('gex', key=lambda x: x.abs(), ascending=False)
    above = agg[agg['strike'] >  spot].sort_values('gex', key=lambda x: x.abs(), ascending=False)
    support    = float(below.iloc[0]['strike']) if not below.empty else None
    resistance = float(above.iloc[0]['strike']) if not above.empty else None

    # Tile: ±8 strikes around spot
    all_s = sorted(agg['strike'].unique())
    ci    = min(range(len(all_s)), key=lambda i: abs(all_s[i] - spot))
    lo, hi = max(0, ci - 8), min(len(all_s), ci + 9)
    tile  = agg[agg['strike'].isin(all_s[lo:hi])].sort_values('strike', ascending=False)

    return {
        'ticker':     ticker,
        'spot':       round(spot, 2),
        'wall':       wall,
        'support':    support,
        'resistance': resistance,
        'net':        round(net),
        'env':        'NEG' if net < 0 else 'POS',
        'strikes':    tile[['strike', 'gex']].to_dict('records'),
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
    isNeg    = d['net'] < 0
    envColor = '#00e5e5' if isNeg else '#e040fb'
    ROW_H, HDR_H, FOOT_H = 20, 30, 44
    LABEL_W, BAR_X, BAR_MAX = 40, 44, 148
    H = HDR_H + len(strikes) * ROW_H + FOOT_H

    spotStrike = min(strikes, key=lambda s: abs(s['strike'] - d['spot']))['strike']

    p = []
    p.append(f'<rect width="{W}" height="{H}" fill="#161616"/>')
    p.append(f'<rect width="{W}" height="{HDR_H}" fill="#1c1c1c"/>')
    p.append(f'<text x="8" y="19" fill="#d4d4d4" font-size="13" font-weight="bold" font-family="monospace">{d["ticker"]}</text>')
    p.append(f'<text x="{W-8}" y="13" text-anchor="end" fill="#888" font-size="10" font-family="monospace">{d["spot"]}</text>')
    env_sym = '●' if isNeg else '◆'
    p.append(f'<text x="{W-8}" y="26" text-anchor="end" fill="{envColor}" font-size="9" font-family="monospace">{"NEG" if isNeg else "POS"} {env_sym}</text>')

    for i, row in enumerate(strikes):
        y      = HDR_H + i * ROW_H
        isWall = row['strike'] == d['wall']
        isSpot = row['strike'] == spotStrike
        isSupp = row['strike'] == d['support']
        isRes  = row['strike'] == d['resistance']

        rowBg = '#1e1a0e' if isSpot else ('#181818' if i % 2 else '#161616')
        p.append(f'<rect x="0" y="{y}" width="{W}" height="{ROW_H}" fill="{rowBg}"/>')
        if isWall:
            p.append(f'<rect x="{LABEL_W}" y="{y}" width="{W-LABEL_W}" height="{ROW_H}" fill="rgba(255,215,0,0.07)"/>')

        sc = '#ffd700' if isWall else ('#fbbf24' if isSpot else '#555')
        p.append(f'<text x="{LABEL_W-3}" y="{y+ROW_H//2+4}" text-anchor="end" fill="{sc}" font-size="10" font-family="monospace">{int(row["strike"])}</text>')

        bw = math.sqrt(abs(row['gex']) / maxAbs) * BAR_MAX
        bc = '#ffd700' if isWall else ('#e040fb' if row['gex'] >= 0 else '#00e5e5')
        if bw > 0.5:
            p.append(f'<rect x="{BAR_X}" y="{y+4}" width="{bw:.1f}" height="{ROW_H-8}" fill="{bc}" opacity="0.85" rx="1"/>')

        if isWall:
            p.append(f'<text x="{W-4}" y="{y+ROW_H//2+4}" text-anchor="end" fill="#ffd700" font-size="9" font-family="monospace">&#9733;</text>')
        elif isRes:
            p.append(f'<text x="{W-4}" y="{y+ROW_H//2+4}" text-anchor="end" fill="#e040fb" font-size="9" font-family="monospace">&#9650; RES</text>')
        elif isSupp:
            p.append(f'<text x="{W-4}" y="{y+ROW_H//2+4}" text-anchor="end" fill="#00e5e5" font-size="9" font-family="monospace">&#9660; SUPP</text>')

        if isSpot:
            p.append(f'<line x1="{BAR_X}" y1="{y+ROW_H//2}" x2="{BAR_X+BAR_MAX}" y2="{y+ROW_H//2}" stroke="#fbbf24" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.7"/>')

    fy = HDR_H + len(strikes) * ROW_H
    p.append(f'<rect x="0" y="{fy}" width="{W}" height="{FOOT_H}" fill="#1a1a1a"/>')
    p.append(f'<line x1="0" y1="{fy}" x2="{W}" y2="{fy}" stroke="#222" stroke-width="1"/>')

    wall_s = str(int(d['wall']))        if d['wall']        is not None else '—'
    res_s  = str(int(d['resistance']))  if d['resistance']  is not None else '—'
    supp_s = str(int(d['support']))     if d['support']     is not None else '—'
    p.append(f'<text x="8" y="{fy+15}" fill="#555" font-size="9" font-family="monospace">&#9733; {wall_s}  &#9650; {res_s}  &#9660; {supp_s}</text>')

    netK   = round(d['net'] / 1000)
    netStr = f'+${abs(netK):,}K' if netK >= 0 else f'-${abs(netK):,}K'
    p.append(f'<text x="8" y="{fy+33}" fill="{envColor}" font-size="12" font-weight="bold" font-family="monospace">NET {netStr}</text>')

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
    lines.append('| Ticker | Spot | Wall | Support | Resistance | Net GEX | Env |')
    lines.append('|--------|------|------|---------|------------|---------|-----|')
    for s in summaries:
        netK = round(s['net'] / 1000)
        wall_s = str(int(s['wall']))       if s['wall']       is not None else '—'
        supp_s = str(int(s['support']))    if s['support']    is not None else '—'
        res_s  = str(int(s['resistance'])) if s['resistance'] is not None else '—'
        lines.append(f"| {s['ticker']} | {s['spot']} | {wall_s} | {supp_s} | {res_s} | ${netK:+,}K | {s['env']} |")

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


def git_push():
    subprocess.run(['git', 'add', 'DAILY.md', 'daily.html'], check=True, cwd=BASE)
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
        earnings = read_earnings()
        events   = read_events()
        write_daily_md(summaries, earnings, events)
        write_gex_html(summaries, earnings, events)
        log.info('Pushing to GitHub...')
        git_push()


if __name__ == '__main__':
    main()
