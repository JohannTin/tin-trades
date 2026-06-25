#!/usr/bin/env python3
"""
EOD check — append intraday price charts with morning GEX levels to daily.html.

Run at 4:15 PM ET after market close.

Input:  data/gamma/gex_levels.db (morning GEX levels per ticker)
        yfinance 5m intraday bars
        daily.html (must exist from daily_report.py morning run)
Output: daily.html with intraday chart grid appended; pushed to GitHub

Usage:
    .venv/bin/python eod_check.py           # skip if market closed
    .venv/bin/python eod_check.py --force   # run regardless
"""
import base64
import io
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yaml
import yfinance as yf

import gex as gexlib
from utils import setup_logger, is_market_open, log_run

BASE    = Path(__file__).parent
TODAY   = date.today().isoformat()
FORCE   = '--force' in sys.argv
DB_PATH = BASE / 'data/gamma/gex_levels.db'

with open(BASE / 'config.yaml') as f:
    cfg = yaml.safe_load(f)

TICKERS = cfg['watchlist'] + cfg.get('mag7', [])
log = setup_logger('eod', prefix='eod')


def load_morning_levels():
    # Load the earliest GEX levels per ticker: intraday → morning table → daily_summary.json fallback.
    # Try intraday first (earliest snap = opening plan)
    all_snaps = gexlib.load_snapshots(DB_PATH, 'intraday', TODAY)
    if all_snaps:
        by_ticker = {}
        for s in all_snaps:
            t = s['ticker']
            if t not in by_ticker:   # first snap per ticker = earliest
                by_ticker[t] = {k: s[k] for k in ('spot','wall_0','support_0','resistance_0','wall_w','support_w','resistance_w')}
        return by_ticker

    # Fall back to morning table
    morning = gexlib.load_snapshots(DB_PATH, 'morning', TODAY)
    if morning:
        return {s['ticker']: {k: s[k] for k in ('spot','wall_0','support_0','resistance_0','wall_w','support_w','resistance_w')}
                for s in morning}

    # Last resort: daily_summary.json
    sjson = BASE / 'data/gamma/daily_summary.json'
    if sjson.exists():
        data = json.loads(sjson.read_text())
        return {t['ticker']: {k: t.get(k) for k in ('spot','wall_0','support_0','resistance_0','wall_w','support_w','resistance_w')}
                for t in data.get('tickers', [])}
    return {}


def load_intraday_trail(ticker):
    # Return all intraday snapshots for ticker today — used to draw the 0DTE wall migration trail.
    return gexlib.load_snapshots(DB_PATH, 'intraday', TODAY, ticker)


def generate_chart(ticker, levels, trail=None):
    # Render 5m candlestick chart with GEX level lines and wall migration trail; returns base64 PNG or None.
    hist = yf.Ticker(ticker).history(period='1d', interval='5m')
    if hist.empty:
        return None

    # Convert to ET, strip timezone so matplotlib renders times as-is
    hist.index = hist.index.tz_convert('America/New_York').tz_localize(None)
    hist = hist.between_time('09:30', '16:00')
    if hist.empty:
        return None

    BG  = '#0e0e0e'
    CW  = pd.Timedelta(minutes=3.5)   # candle body width
    fig, ax = plt.subplots(figsize=(5.5, 2.8))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor('#161616')

    for ts, row in hist.iterrows():
        o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
        col = '#26a69a' if c >= o else '#ef5350'
        ax.vlines(ts, l, h, color=col, linewidth=0.8, zorder=3)
        ax.bar(ts, max(abs(c - o), 0.01), bottom=min(o, c),
               width=CW, color=col, linewidth=0, zorder=3)

    xmin = hist.index[0]

    wall_0 = levels.get('wall_0');   supp_0 = levels.get('support_0');   res_0 = levels.get('resistance_0')
    wall_w = levels.get('wall_w');   supp_w = levels.get('support_w');   res_w = levels.get('resistance_w')
    spot   = levels.get('spot')

    drawn = set()
    def _hline(val, color, lw, ls, alpha, label, va='bottom'):
        if val is None or val in drawn: return
        drawn.add(val)
        ax.axhline(val, color=color, linewidth=lw, linestyle=ls, alpha=alpha, zorder=4)
        ax.text(xmin, val, f' {label}{int(val)}', color=color, fontsize=7, va=va, fontfamily='monospace')

    # 0DTE: solid lines (bright)
    _hline(wall_0, '#ffd700', 1.3, '-',  0.95, '★')
    _hline(supp_0, '#00e5e5', 1.0, '-',  0.85, '▼', va='top')
    _hline(res_0,  '#e040fb', 1.0, '-',  0.85, '▲')
    # Weekly: dashed, dimmer
    _hline(wall_w, '#8a7000', 1.0, '--', 0.7,  '◆')
    _hline(supp_w, '#006060', 0.8, '--', 0.6,  '▽', va='top')
    _hline(res_w,  '#6a1060', 0.8, '--', 0.6,  '△')
    # Open spot
    if spot:
        ax.axhline(spot, color='#fbbf24', linewidth=0.8, linestyle=':', alpha=0.4, zorder=4)

    # Wall migration trail — dotted segments showing 0DTE wall at each snapshot
    if trail and len(trail) >= 2:
        times = []
        walls = []
        for snap in trail:
            try:
                t = datetime.strptime(f'{TODAY} {snap["snap_time"]}', '%Y-%m-%d %H:%M')
                w = snap.get('wall_0')
                if w:
                    times.append(t)
                    walls.append(w)
            except Exception:
                pass
        if len(times) >= 2:
            # Draw step segments: wall held from snap_time to next snap_time
            for i in range(len(times) - 1):
                if walls[i] != walls[i + 1]:   # only draw when wall migrates
                    ax.hlines(walls[i], times[i], times[i + 1],
                              colors='#5a4a00', linewidth=1.5, linestyle=':', alpha=0.6, zorder=2)
            ax.hlines(walls[-1], times[-1], hist.index[-1],
                      colors='#5a4a00', linewidth=1.5, linestyle=':', alpha=0.6, zorder=2)

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_minor_locator(mdates.MinuteLocator(byminute=[30]))
    ax.tick_params(axis='x', which='major', colors='#555', labelsize=7)
    ax.tick_params(axis='x', which='minor', colors='#333', length=3)
    ax.tick_params(axis='y', colors='#444', labelsize=7)
    for spine in ax.spines.values():
        spine.set_color('#2a2a2a')
    ax.set_title(ticker, color='#888', fontsize=9, fontfamily='monospace', pad=3, loc='left')
    ax.grid(axis='y', color='#1e1e1e', linewidth=0.5, zorder=1)
    ax.set_xlim(xmin - pd.Timedelta(minutes=5), hist.index[-1] + pd.Timedelta(minutes=5))

    plt.tight_layout(pad=0.4)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, facecolor=BG, bbox_inches='tight')
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def build_charts_section(levels):
    # Generate chart HTML for every ticker and wrap in a grid section div.
    charts_html = ''
    for ticker in TICKERS:
        lv    = levels.get(ticker, {})
        trail = load_intraday_trail(ticker)
        b64   = generate_chart(ticker, lv, trail)
        if b64:
            charts_html += f'<div class="tile"><img src="data:image/png;base64,{b64}" style="width:100%;display:block;border-radius:3px"/></div>'
            log.info(f'{ticker}: chart generated')
        else:
            charts_html += f'<div class="tile" style="background:#161616;padding:8px;color:#444;font-size:10px;font-family:monospace">{ticker} no data</div>'
            log.warning(f'{ticker}: no intraday data')

    return f'''
<div class="gex-wrap" style="margin-top:24px">
  <h2>Intraday — {TODAY} (GEX levels: ★ wall  ▲ res  ▼ supp  ·· open spot)</h2>
  <div class="gex-grid">
    {charts_html}
  </div>
</div>'''


def append_charts_to_html(section_html):
    # Inject the chart section before </body> in daily.html in-place.
    html_path = BASE / 'daily.html'
    html = html_path.read_text()
    html = html.replace('</body>', section_html + '\n</body>')
    html_path.write_text(html)


def git_push():
    # Stage daily.html, commit if changed, and push to GitHub.
    subprocess.run(['git', 'add', 'daily.html'], check=True, cwd=BASE)
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=BASE).returncode != 0:
        subprocess.run(['git', 'commit', '-m', f'eod: {TODAY}'], check=True, cwd=BASE)
    subprocess.run(['git', 'push'], check=True, cwd=BASE)


def main():
    # Orchestrate EOD: load levels, generate charts, append to daily.html, push.
    with log_run(log, 'eod_check'):
        if not is_market_open() and not FORCE:
            log.info('Market closed today. Skipping. (--force to override)')
            return

        if not (BASE / 'daily.html').exists():
            log.error('daily.html not found — did daily_report.py run this morning?')
            return

        levels = load_morning_levels()
        if not levels:
            log.warning('No morning levels in gex_levels.db — charts will have no GEX lines')

        section = build_charts_section(levels)
        append_charts_to_html(section)
        log.info('Pushing updated daily.html to GitHub...')
        git_push()


if __name__ == '__main__':
    main()
