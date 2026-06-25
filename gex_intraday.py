#!/usr/bin/env python3
"""
GEX intraday snapshots — fetch real IBKR greeks every 30 min, 9:15–3:30 ET.

Fires via launchctl at: 6:15 6:30 7:00 7:30 8:00 8:30 9:00 9:30 10:00 10:30 11:00 11:30 12:00 12:30 PT
Writes one row per ticker to data/gamma/gex_levels.db [intraday] table.

Usage:
    .venv/bin/python gex_intraday.py           # skip if market closed
    .venv/bin/python gex_intraday.py --force   # run regardless
"""
import sys
import yaml
from datetime import date, datetime
from pathlib import Path

from ib_insync import IB

import gex
from utils import setup_logger, is_market_open, log_run

BASE    = Path(__file__).parent
TODAY   = date.today().isoformat()
FORCE   = '--force' in sys.argv
DB_PATH = BASE / 'data/gamma/gex_levels.db'

with open(BASE / 'config.yaml') as f:
    cfg = yaml.safe_load(f)

IBKR        = cfg['ibkr']
TICKERS     = cfg['watchlist'] + cfg.get('mag7', [])
STRIKES_PCT = IBKR.get('gex_strikes_pct', 0.08)

log = setup_logger('gex_intraday', prefix='gex_intraday')


def snapshot_ticker(ib, ticker, snap_time):
    spot = gex.get_spot(ticker)
    if not spot:
        log.warning(f'{ticker}: no spot price')
        return

    oi = gex.load_oi(ticker)
    if oi is None:
        log.warning(f'{ticker}: no OI — run options_data.py first')
        return

    oi_0 = oi[oi['expiry'] == TODAY]
    oi_w = oi[oi['expiry'] >  TODAY]

    # Single IBKR fetch covers all expiries; split by expiry after
    gamma_df = gex.fetch_ibkr_greeks(ib, ticker, spot, oi, STRIKES_PCT)
    n_greeks = len(gamma_df)

    if gamma_df.empty:
        log.warning(f'{ticker}: no IBKR greeks — falling back to BS')

    def _compute(oi_slice, label):
        if oi_slice.empty:
            return None, None, None, 0.0
        g = gamma_df[gamma_df['expiry'].isin(oi_slice['expiry'].unique())] if not gamma_df.empty else gamma_df
        if not g.empty:
            gex_df = gex.ibkr_to_gex(oi_slice, g, spot)
        else:
            gex_df = gex.chain_to_gex(oi_slice, spot)
        return gex.levels(gex_df, spot)

    lvl_0 = _compute(oi_0, '0DTE')
    lvl_w = _compute(oi_w, 'weekly')

    gex.save_snapshot(DB_PATH, 'intraday', TODAY, ticker, snap_time, spot, lvl_0, lvl_w)

    w0, s0, r0, n0 = lvl_0
    ww, sw, rw, nw = lvl_w
    src = f'IBKR({n_greeks})' if n_greeks else 'BS'
    log.info(f'{ticker} {snap_time} [{src}] spot={spot} '
             f'0D wall={w0} s={s0} r={r0} net={round(n0/1e9,2) if n0 else 0}B | '
             f'Wk wall={ww} s={sw} r={rw} net={round(nw/1e9,2) if nw else 0}B')


def main():
    with log_run(log, 'gex_intraday'):
        if not is_market_open() and not FORCE:
            log.info('Market closed. Skipping. (--force to override)')
            return

        snap_time = datetime.now().strftime('%H:%M')

        ib = IB()
        ib.connect(IBKR['host'], IBKR['port'], clientId=12, readonly=True)
        log.info(f'Connected: {ib.managedAccounts()} — snapshot {snap_time}')
        try:
            for ticker in TICKERS:
                snapshot_ticker(ib, ticker, snap_time)
        finally:
            ib.disconnect()


if __name__ == '__main__':
    main()
