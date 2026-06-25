#!/usr/bin/env python3
"""
GEX snapshot via TWS/IBKR OPRA — runs at 9:05 AM ET, before market open.

Computes gamma flip level (BS) + real greeks (IBKR) for all 10 tickers.
Writes per-ticker parquet + summary JSON. DB snapshot is handled by gex_intraday.py.

Requires: TWS running with OPRA subscription.
"""
import json
import yaml
import pandas as pd
from pathlib import Path
from datetime import date, datetime, timezone

import gex
from ib_insync import IB
from utils import setup_logger, is_market_open, log_run

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

IBKR        = cfg['ibkr']
TICKERS     = cfg['watchlist'] + cfg.get('mag7', [])
STRIKES_PCT = IBKR.get('gex_strikes_pct', 0.08)
TODAY       = date.today().isoformat()
YEAR        = date.today().year

log = setup_logger('gamma', prefix='gamma')


def gex_path(ticker):
    p = Path(f'data/gamma/{ticker}_gex_{YEAR}.parquet')
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def compute_flip(oi_df, spot):
    """Gamma flip via cumulative BS GEX across all strikes."""
    now  = datetime.now(timezone.utc)
    rows = []
    for _, row in oi_df.iterrows():
        try:
            exp_dt = datetime.fromisoformat(row['expiry']).replace(hour=20, tzinfo=timezone.utc)
            T = max((exp_dt - now).total_seconds() / (365.25 * 24 * 3600), 1 / (365 * 24))
        except Exception:
            continue
        iv = ((row.get('call_iv') or 0) + (row.get('put_iv') or 0)) / 2
        if iv <= 0:
            continue
        g = gex.bs_gamma(spot, row['strike'], T, iv)
        rows.append({'strike': row['strike'], 'gex': (row['call_oi'] - row['put_oi']) * g * 100 * spot})

    if not rows:
        return None, None

    df = pd.DataFrame(rows).groupby('strike')['gex'].sum().reset_index().sort_values('strike')
    df['cum'] = df['gex'].cumsum()
    flips = df[df['cum'].shift(1, fill_value=df['cum'].iloc[0]) * df['cum'] < 0]
    if not flips.empty:
        return float(flips['strike'].iloc[0]), None
    zone = 'above' if df['cum'].iloc[-1] < 0 else 'below'
    return None, zone


def process_ticker(ib, ticker):
    oi_df = gex.load_oi(ticker)
    if oi_df is None:
        log.error(f'{ticker}: no OI — run options_data.py first')
        return

    spot = gex.get_spot(ticker)
    if not spot:
        log.error(f'{ticker}: no spot price')
        return
    log.info(f'{ticker} spot: {spot:.2f}')

    flip, flip_zone = compute_flip(oi_df, spot)
    log.info(f'{ticker} flip: {flip}  zone: {flip_zone}')

    gamma_df = gex.fetch_ibkr_greeks(ib, ticker, spot, oi_df, STRIKES_PCT)
    if gamma_df.empty:
        log.error(f'{ticker}: no IBKR greeks — check TWS and OPRA subscription')
        return
    log.info(f'{ticker}: greeks for {len(gamma_df)} (expiry, strike) pairs')

    gex_df    = gex.ibkr_to_gex(oi_df, gamma_df, spot)
    wall, support, resistance, total_gex = gex.levels(gex_df, spot)

    # Persist per-ticker GEX parquet
    out = gex_df.assign(date=TODAY, spot=spot)[['date', 'spot', 'expiry', 'strike', 'gex']]
    p   = gex_path(ticker)
    if p.exists():
        existing = pd.read_parquet(p)
        out = pd.concat([existing[existing['date'] != TODAY], out], ignore_index=True)
    out.to_parquet(p, index=False)

    # Per-ticker summary JSON
    flip_str = f'{flip:.2f}' if flip else (f'{flip_zone} range' if flip_zone else 'N/A')
    summary  = {
        'date': TODAY, 'spot': round(spot, 2),
        'flip': round(flip, 2) if flip else None, 'flip_zone': flip_zone,
        'wall': round(wall, 2) if wall else None,
        'support': round(support, 2) if support else None,
        'resistance': round(resistance, 2) if resistance else None,
        'total_gex': round(total_gex / 1e9, 3),
    }
    Path(f'data/gamma/{ticker}_summary.json').write_text(json.dumps(summary))

    sign = 'NEG (vol AMP)' if total_gex < 0 else 'POS (vol PIN)'
    log.info(f'GAMMA {ticker} | spot={spot:.2f} flip={flip_str} '
             f'wall={wall} supp={support} res={resistance} '
             f'GEX={total_gex/1e9:+.2f}B {sign}')


def main():
    with log_run(log, 'gamma_exposure'):
        if not is_market_open():
            log.info('Market closed today. Exiting.')
            return

        ib = IB()
        ib.connect(IBKR['host'], IBKR['port'], clientId=IBKR['client_id'],
                   readonly=IBKR.get('readonly', False))
        try:
            for ticker in TICKERS:
                process_ticker(ib, ticker)
        finally:
            ib.disconnect()


if __name__ == '__main__':
    main()
