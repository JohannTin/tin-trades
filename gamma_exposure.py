#!/usr/bin/env python3
"""
GEX (Gamma Exposure) snapshot for watchlist tickers via TWS/IBKR OPRA.

Run at 9:05 AM ET — 5 min after options_data.py captures today's OI.
Requires: TWS or IB Gateway running with OPRA subscription.

Output: data/gamma/{ticker}_gex_{year}.parquet + data/gamma/{ticker}_summary.json + Telegram.
"""
import json
import math
import yaml
import pandas as pd
from pathlib import Path
from datetime import date

import yfinance as yf
from ib_insync import IB, Option
from utils import setup_logger, is_market_open, log_run

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

IBKR        = cfg['ibkr']
WATCHLIST   = cfg['watchlist']
STRIKES_PCT = IBKR.get('gex_strikes_pct', 0.08)
TODAY       = date.today().isoformat()
YEAR        = date.today().year

log = setup_logger('gamma', prefix='gamma')


def gex_path(ticker):
    p = Path(f'data/gamma/{ticker}_gex_{YEAR}.parquet')
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_oi(ticker):
    p = Path(f'data/options/{ticker}_chain_{YEAR}.parquet')
    if not p.exists():
        return None
    df    = pd.read_parquet(p)
    cols  = ['expiry', 'strike', 'call_oi', 'put_oi', 'call_iv', 'put_iv']
    today = df[df['date'] == TODAY][cols]
    return today if not today.empty else None


def get_spot(ticker):
    # ponytail: yfinance avoids needing a separate US equity subscription
    info = yf.Ticker(ticker).fast_info
    return info.get('lastPrice') or info.get('previousClose')


def bs_gamma(S, K, T, sigma, r=0.05):
    """Black-Scholes gamma — used for flip detection across all strikes."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 ** 2) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))


def compute_flip(oi_df, spot):
    """
    Find gamma flip using BS gamma + OI across all available strikes.
    Returns (flip_strike, flip_zone) where flip_zone is 'above'/'below' when
    the flip is outside the available strike range (no crossing found).
    """
    today_date = date.today()
    rows = []
    for _, row in oi_df.iterrows():
        try:
            T = max((date.fromisoformat(row['expiry']) - today_date).days / 365, 1 / 365)
        except Exception:
            continue
        iv = ((row.get('call_iv') or 0) + (row.get('put_iv') or 0)) / 2
        if iv <= 0:
            continue
        gamma = bs_gamma(spot, row['strike'], T, iv)
        rows.append({'strike': row['strike'],
                     'gex':    (row['call_oi'] - row['put_oi']) * gamma * 100 * spot})

    if not rows:
        return None, None

    df  = pd.DataFrame(rows).groupby('strike')['gex'].sum().reset_index().sort_values('strike')
    df['cum_gex'] = df['gex'].cumsum()
    shifted   = df['cum_gex'].shift(1, fill_value=df['cum_gex'].iloc[0])
    flip_rows = df[shifted * df['cum_gex'] < 0]

    if not flip_rows.empty:
        return float(flip_rows['strike'].iloc[0]), None

    # No crossing: cumulative stays on one side across all strikes
    zone = 'above' if df['cum_gex'].iloc[-1] < 0 else 'below'
    return None, zone


def fetch_gammas(ib, ticker, spot, oi_df):
    """
    Request call modelGreeks for each (expiry, strike) pair near ATM.
    Gamma is identical for calls and puts at same strike/expiry (put-call parity),
    so we request only calls to halve the number of TWS subscriptions.
    """
    lo, hi = spot * (1 - STRIKES_PCT), spot * (1 + STRIKES_PCT)
    pairs  = (oi_df[(oi_df['strike'] >= lo) & (oi_df['strike'] <= hi)]
              [['expiry', 'strike']].drop_duplicates())

    contracts = [
        Option(ticker, row.expiry.replace('-', ''), row.strike, 'C', 'SMART', multiplier='100')
        for row in pairs.itertuples()
    ]
    log.info(f'Qualifying {len(contracts)} contracts...')
    valid = ib.qualifyContracts(*contracts)
    log.info(f'{len(valid)} qualified')

    tickers = []
    BATCH   = 60
    for i in range(0, len(valid), BATCH):
        for c in valid[i:i + BATCH]:
            tickers.append(ib.reqMktData(c, '', snapshot=True))
        ib.sleep(5)

    rows, missing = [], 0
    for t in tickers:
        c = t.contract
        g = t.modelGreeks
        if not g or g.gamma is None:
            missing += 1
            continue
        exp = c.lastTradeDateOrContractMonth
        rows.append({
            'expiry': f'{exp[:4]}-{exp[4:6]}-{exp[6:8]}',
            'strike': c.strike,
            'gamma':  g.gamma,
        })

    if missing:
        log.warning(f'{missing} contracts returned no greeks')
    return pd.DataFrame(rows)


def compute_gex(oi_df, gamma_df, spot):
    df = oi_df.merge(gamma_df, on=['expiry', 'strike'], how='inner')
    df['gex'] = (df['call_oi'] - df['put_oi']) * df['gamma'] * 100 * spot
    agg  = df.groupby('strike')['gex'].sum().reset_index()
    wall = float(agg.loc[agg['gex'].abs().idxmax(), 'strike'])
    return df, wall


def process_ticker(ib, ticker):
    oi_df = load_oi(ticker)
    if oi_df is None:
        log.error(f'{ticker}: no OI for today — run options_data.py first.')
        return

    spot = get_spot(ticker)
    if not spot or spot != spot:
        log.error(f'{ticker}: could not get spot price.')
        return
    log.info(f'{ticker} spot: {spot:.2f}')

    flip, flip_zone = compute_flip(oi_df, spot)
    log.info(f'{ticker} BS flip: {flip}  zone: {flip_zone}')

    gamma_df = fetch_gammas(ib, ticker, spot, oi_df)
    if gamma_df.empty:
        log.error(f'{ticker}: no gamma data returned. Check TWS connection and OPRA subscription.')
        return
    log.info(f'{ticker}: gamma received for {len(gamma_df)} (expiry, strike) pairs.')

    detail_df, wall = compute_gex(oi_df, gamma_df, spot)
    total_gex       = detail_df['gex'].sum()

    detail_df.insert(0, 'date', TODAY)
    detail_df.insert(1, 'spot', spot)
    p = gex_path(ticker)
    if p.exists():
        existing  = pd.read_parquet(p)
        detail_df = pd.concat([existing[existing['date'] != TODAY], detail_df],
                              ignore_index=True)
    detail_df.to_parquet(p, index=False)

    summary_data = {
        'date':      TODAY,
        'spot':      round(spot, 2),
        'flip':      round(flip, 2) if flip else None,
        'flip_zone': flip_zone,
        'wall':      round(wall, 2),
        'total_gex': round(total_gex / 1e9, 3),
    }
    Path(f'data/gamma/{ticker}_summary.json').write_text(json.dumps(summary_data))

    sign     = 'neg (vol AMP)' if total_gex < 0 else 'pos (vol PIN)'
    flip_str = (f'{flip:.2f}' if flip
                else f'{flip_zone} range' if flip_zone else 'N/A')
    log.info('\n' + (
        f'GAMMA  {ticker}  {TODAY}\n'
        f'Spot  {spot:.2f}\n'
        f'Flip  {flip_str}\n'
        f'Wall  {wall:.2f}\n'
        f'GEX   {total_gex / 1e9:+.2f}B  {sign}'
    ))


def main():
    with log_run(log, 'gamma_exposure'):
        if not is_market_open():
            log.info('Market closed today. Exiting.')
            return

        ib = IB()
        ib.connect(IBKR['host'], IBKR['port'], clientId=IBKR['client_id'],
                   readonly=IBKR.get('readonly', False))
        try:
            for ticker in WATCHLIST:
                process_ticker(ib, ticker)
        finally:
            ib.disconnect()


if __name__ == '__main__':
    main()
