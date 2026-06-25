#!/usr/bin/env python3
"""
Trade logger — polls IBKR for fills every 5 minutes and persists to SQLite.

Input:  IBKR TWS executions via ib_insync reqExecutions()
Output: data/trades.db (SQLite) — one row per execution, keyed by exec_id

Run via launchctl (com.tintrades.trades) or manually:
  set -a && source .env && set +a && .venv/bin/python trades.py
"""
import os
import sqlite3
import sys
import time
import yaml
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ponytail: launchd can't chdir to ~/Documents (TCC), so Python does it instead
ROOT = Path(__file__).parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from utils import setup_logger

with open('config.yaml') as f:
    cfg = yaml.safe_load(f)

IBKR     = cfg['ibkr']
ET       = ZoneInfo('America/New_York')
INTERVAL = 300  # seconds
DB       = 'data/trades.db'
log      = setup_logger('trades', prefix='trades')


def init_db():
    # Create the trades table if it doesn't exist yet.
    con = sqlite3.connect(DB)
    con.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            exec_id   TEXT PRIMARY KEY,
            dt        TEXT,
            symbol    TEXT,
            sec_type  TEXT,
            expiry    TEXT,
            strike    REAL,
            right     TEXT,
            side      TEXT,
            qty       REAL,
            price     REAL,
            avg_price REAL,
            order_id  INTEGER
        )
    ''')
    con.commit()
    con.close()


def save(trade):
    # INSERT OR IGNORE one fill dict; returns 1 if new, 0 if already stored.
    con = sqlite3.connect(DB)
    con.execute('''
        INSERT OR IGNORE INTO trades
        VALUES (:exec_id,:dt,:symbol,:sec_type,:expiry,:strike,:right,:side,:qty,:price,:avg_price,:order_id)
    ''', trade)
    saved = con.total_changes
    con.commit()
    con.close()
    return saved


def parse(fill):
    # Extract contract + execution fields from an ib_insync Fill into a flat dict.
    c, e = fill.contract, fill.execution
    dt = e.time if e.time.tzinfo else e.time.replace(tzinfo=ET)
    return {
        'exec_id':   e.execId,
        'dt':        dt.isoformat(),
        'symbol':    c.symbol,
        'sec_type':  c.secType,
        'expiry':    c.lastTradeDateOrContractMonth or None,
        'strike':    c.strike or None,
        'right':     c.right or None,
        'side':      e.side,
        'qty':       e.shares,
        'price':     e.price,
        'avg_price': e.avgPrice,
        'order_id':  e.orderId,
    }


def poll_once():
    # Connect to IBKR, fetch all today's executions, save any new ones; disconnect on any error.
    # ponytail: import inside function so launchd doesn't choke on ib_insync's asyncio setup at module load
    from ib_insync import IB
    ib = IB()
    try:
        ib.connect(IBKR['host'], IBKR['port'], clientId=IBKR['client_id'] + 1, readonly=True)
        fills = ib.reqExecutions()
        new = 0
        for fill in fills:
            new += save(parse(fill))
        if new:
            log.info(f'{new} new fill(s) saved ({len(fills)} total today)')
    except Exception as e:
        log.error(f'poll error: {e}')
    finally:
        ib.disconnect()


def main():
    # Init DB and loop poll_once every INTERVAL seconds indefinitely.
    init_db()
    log.info(f'Trade logger started — polling every {INTERVAL}s')
    while True:
        poll_once()
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
