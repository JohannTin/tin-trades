#!/usr/bin/env python3
"""
Intraday price data collector — downloads 1m OHLCV bars for the watchlist.

Input:  config.yaml (watchlist, price_only, prepost); yfinance
Output: data/candles/<TICKER>_<YEAR>.parquet — one row per 1m bar, appended daily

Usage:
    .venv/bin/python market_data.py   # skips if market closed
"""
import time
import yaml
import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import date

from utils import setup_logger, is_market_open, log_run

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

WATCHLIST  = cfg['watchlist'] + cfg.get('price_only', [])
PREPOST    = cfg.get('prepost', True)
TODAY_FMT = date.today().strftime('%Y-%m-%d')

log = setup_logger()


def parquet_path(ticker):
    # Return the annual candle parquet path, creating parent dir if needed.
    p = Path(f'data/candles/{ticker}_{date.today().year}.parquet')
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def already_fetched(ticker):
    # Return True if today's date appears in the existing parquet (avoids duplicate fetches).
    p = parquet_path(ticker)
    if not p.exists():
        return False
    df = pd.read_parquet(p, columns=['datetime'])
    return df['datetime'].astype(str).str.startswith(TODAY_FMT).any()


def save(df, ticker):
    # Merge new 1m bars into the annual parquet, deduplicating by datetime.
    p = parquet_path(ticker)
    if p.exists():
        existing = pd.read_parquet(p)
        df = pd.concat([existing, df]).drop_duplicates('datetime').reset_index(drop=True)
    df.to_parquet(p, index=False)


def fetch_underlyings():
    # Download today's 1m OHLCV bars for the watchlist, skipping tickers already fetched today.
    t0 = time.time()
    fetched = skipped = failed = total_rows = 0

    for ticker in WATCHLIST:
        if already_fetched(ticker):
            skipped += 1
            continue
        try:
            hist = yf.Ticker(ticker).history(period='1d', interval='1m', prepost=PREPOST)
            if hist.empty:
                log.warning(f'{ticker}: no data')
                failed += 1
                continue
            hist = hist.reset_index()[['Datetime', 'Open', 'High', 'Low', 'Close', 'Volume']]
            hist.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']
            hist['datetime'] = hist['datetime'].astype(str)
            save(hist, ticker)
            total_rows += len(hist)
            fetched += 1
        except Exception as e:
            log.error(f'{ticker}: {e}')
            failed += 1

    parts = []
    if fetched:  parts.append(f'{fetched} tickers, {total_rows} rows')
    if skipped:  parts.append(f'{skipped} skipped')
    if failed:   parts.append(f'{failed} failed')
    log.info(f'Underlying 1m candles: {", ".join(parts) or "nothing to do"} ({time.time()-t0:.1f}s)')
    log.info(f'Tickers fetched: {", ".join(WATCHLIST)}')


def main():
    # Guard on market open, then run fetch_underlyings.
    with log_run(log, 'market_data'):
        if not is_market_open():
            log.info('Market closed today. Exiting.')
            return
        fetch_underlyings()


if __name__ == '__main__':
    main()
