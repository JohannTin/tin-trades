#!/usr/bin/env python3
"""
Options chain snapshot.

Usage:
    .venv/bin/python options_data.py           # 9:00 AM — full chain (OI, IV, volume, bid/ask)
    .venv/bin/python options_data.py --quotes  # 9:35 AM — update bid/ask only
"""
import sys
import time
import yaml
import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import date

from utils import setup_logger, is_market_open, log_run

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

WATCHLIST = cfg['watchlist'] + cfg.get('mag7', [])
TODAY     = date.today().isoformat()
YEAR      = date.today().year
QUOTES    = '--quotes' in sys.argv

log = setup_logger('options', prefix='options')


def chain_path(ticker):
    p = Path(f'data/options/{ticker}_chain_{YEAR}.parquet')
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def get_expiries(ticker):
    exps  = yf.Ticker(ticker).options
    today = date.today().isoformat()
    result = []
    if today in exps:
        result.append(today)
    friday = next((e for e in exps if e > today and
                   date.fromisoformat(e).weekday() == 4), None)
    if friday and friday not in result:
        result.append(friday)
    return result or [exps[0]]


def fetch_chain(ticker):
    t0       = time.time()
    expiries = get_expiries(ticker)
    tkr      = yf.Ticker(ticker)
    rows     = []

    for exp in expiries:
        chain = tkr.option_chain(exp)
        calls = chain.calls[['strike', 'openInterest', 'impliedVolatility',
                              'bid', 'ask', 'volume']].copy()
        puts  = chain.puts[['strike',  'openInterest', 'impliedVolatility',
                             'bid', 'ask', 'volume']].copy()
        calls.columns = ['strike', 'call_oi', 'call_iv', 'call_bid', 'call_ask', 'call_volume']
        puts.columns  = ['strike', 'put_oi',  'put_iv',  'put_bid',  'put_ask',  'put_volume']
        merged = calls.merge(puts, on='strike')
        merged.insert(0, 'date',   TODAY)
        merged.insert(1, 'expiry', exp)
        rows.append(merged)

    df = pd.concat(rows, ignore_index=True)
    p  = chain_path(ticker)
    if p.exists():
        existing = pd.read_parquet(p)
        df = pd.concat([existing[existing['date'] != TODAY], df], ignore_index=True)
    df.to_parquet(p, index=False)
    log.info(f'{ticker} chain: {len(df[df["date"]==TODAY])} rows, expiries={expiries} ({time.time()-t0:.1f}s)')


def update_quotes(ticker):
    t0 = time.time()
    p  = chain_path(ticker)
    if not p.exists():
        log.warning(f'{ticker}: no chain file, run full fetch first')
        return

    existing = pd.read_parquet(p)
    today_rows = existing[existing['date'] == TODAY]
    if today_rows.empty:
        log.warning(f'{ticker}: no rows for today, run full fetch first')
        return

    expiries = today_rows['expiry'].unique().tolist()
    tkr      = yf.Ticker(ticker)

    for exp in expiries:
        chain = tkr.option_chain(exp)
        calls = chain.calls[['strike', 'bid', 'ask']].rename(
            columns={'bid': 'call_bid', 'ask': 'call_ask'})
        puts  = chain.puts[['strike',  'bid', 'ask']].rename(
            columns={'bid': 'put_bid',  'ask': 'put_ask'})
        quotes = calls.merge(puts, on='strike')

        mask = (existing['date'] == TODAY) & (existing['expiry'] == exp)
        existing = existing.merge(
            quotes.rename(columns={'call_bid': '_cb', 'call_ask': '_ca',
                                   'put_bid':  '_pb', 'put_ask':  '_pa'}),
            on='strike', how='left'
        )
        for old, new in [('call_bid','_cb'),('call_ask','_ca'),
                         ('put_bid', '_pb'),('put_ask', '_pa')]:
            existing.loc[mask, old] = existing.loc[mask, new]
            existing.drop(columns=[new], inplace=True, errors='ignore')

    existing.to_parquet(p, index=False)
    log.info(f'{ticker} quotes updated ({time.time()-t0:.1f}s)')


def main():
    label = 'options --quotes' if QUOTES else 'options'
    with log_run(log, label):
        if not is_market_open():
            log.info('Market closed today. Exiting.')
            return
        for ticker in WATCHLIST:
            if QUOTES:
                update_quotes(ticker)
            else:
                fetch_chain(ticker)


if __name__ == '__main__':
    main()
