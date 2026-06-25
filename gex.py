"""GEX computation utilities — shared across options_data, gamma_exposure, daily_report, eod_check."""
import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
from ib_insync import Option

R = 0.05  # risk-free rate


# ── Core maths ────────────────────────────────────────────────────────────────

def bs_gamma(S, K, T, sigma):
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return math.exp(-0.5 * d1 ** 2) / (math.sqrt(2 * math.pi) * S * sigma * math.sqrt(T))


# ── Data loading ──────────────────────────────────────────────────────────────

def get_spot(ticker):
    d = dict(yf.Ticker(ticker).fast_info)
    return float(d.get('lastPrice') or d.get('previousClose') or 0)


def load_oi(ticker, today=None, year=None):
    today = today or date.today().isoformat()
    year  = year  or date.today().year
    p = Path(f'data/options/{ticker}_chain_{year}.parquet')
    if not p.exists():
        return None
    df   = pd.read_parquet(p)
    rows = df[df['date'] == today][['expiry', 'strike', 'call_oi', 'put_oi', 'call_iv', 'put_iv']]
    return rows.copy() if not rows.empty else None


# ── GEX computation ───────────────────────────────────────────────────────────

def chain_to_gex(oi_df, spot, as_of=None):
    """BS GEX from chain OI + IV. as_of=datetime(utc) for T calculation."""
    ref  = as_of or datetime.now(timezone.utc)
    rows = []
    for _, row in oi_df.iterrows():
        # 4 PM ET = 20:00 UTC
        exp_dt = datetime.fromisoformat(str(row['expiry'])).replace(hour=20, tzinfo=timezone.utc)
        T  = max((exp_dt - ref).total_seconds() / (365.25 * 24 * 3600), 1 / (365 * 24))
        cg = bs_gamma(spot, row['strike'], T, float(row.get('call_iv') or 0))
        pg = bs_gamma(spot, row['strike'], T, float(row.get('put_iv') or 0))
        rows.append({
            'strike': float(row['strike']),
            'expiry': str(row['expiry']),
            'gex':    (cg * float(row.get('call_oi') or 0) - pg * float(row.get('put_oi') or 0)) * 100 * spot ** 2,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['strike', 'expiry', 'gex'])


def ibkr_to_gex(oi_df, gamma_df, spot):
    """Merge OI + IBKR greeks into a GEX DataFrame."""
    if gamma_df.empty:
        return pd.DataFrame(columns=['strike', 'expiry', 'gex'])
    df = oi_df.merge(gamma_df, on=['expiry', 'strike'], how='inner')
    df['gex'] = (df['call_oi'] - df['put_oi']) * df['gamma'] * 100 * spot
    return df[['strike', 'expiry', 'gex']]


def levels(gex_df, spot):
    """(wall, supp, res, net) from any GEX DataFrame."""
    if gex_df.empty:
        return None, None, None, 0.0
    agg   = gex_df.groupby('strike')['gex'].sum().reset_index()
    wall  = float(agg.loc[agg['gex'].abs().idxmax(), 'strike'])
    net   = float(agg['gex'].sum())
    below = agg[agg['strike'] <= spot].sort_values('gex', key=lambda x: x.abs(), ascending=False)
    above = agg[agg['strike'] >  spot].sort_values('gex', key=lambda x: x.abs(), ascending=False)
    return (wall,
            float(below.iloc[0]['strike']) if not below.empty else None,
            float(above.iloc[0]['strike']) if not above.empty else None,
            net)


# ── IBKR greeks ───────────────────────────────────────────────────────────────

def fetch_ibkr_greeks(ib, ticker, spot, oi_df, strikes_pct=0.08, batch=60, sleep_s=4):
    """Request call modelGreeks for all (expiry, strike) pairs within ±strikes_pct of spot.
    Gamma is identical for calls/puts (put-call parity) so we request calls only.
    """
    lo, hi = spot * (1 - strikes_pct), spot * (1 + strikes_pct)
    pairs  = (oi_df[(oi_df['strike'] >= lo) & (oi_df['strike'] <= hi)]
              [['expiry', 'strike']].drop_duplicates())
    if pairs.empty:
        return pd.DataFrame(columns=['expiry', 'strike', 'gamma'])

    contracts = [
        Option(ticker, r.expiry.replace('-', ''), r.strike, 'C', 'SMART', multiplier='100')
        for r in pairs.itertuples()
    ]
    valid = ib.qualifyContracts(*contracts)

    tkrs = []
    for i in range(0, len(valid), batch):
        for c in valid[i:i + batch]:
            tkrs.append(ib.reqMktData(c, '', snapshot=True))
        ib.sleep(sleep_s)

    rows = []
    for t in tkrs:
        g = t.modelGreeks
        if not g or g.gamma is None:
            continue
        c   = t.contract
        exp = c.lastTradeDateOrContractMonth
        rows.append({
            'expiry': f'{exp[:4]}-{exp[4:6]}-{exp[6:8]}',
            'strike': c.strike,
            'gamma':  g.gamma,
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=['expiry', 'strike', 'gamma'])


# ── DB persistence ────────────────────────────────────────────────────────────

_INTRADAY_DDL = '''CREATE TABLE IF NOT EXISTS intraday (
    date TEXT, ticker TEXT, snap_time TEXT, spot REAL,
    wall_0 REAL, support_0 REAL, resistance_0 REAL, net_0 REAL,
    wall_w REAL, support_w REAL, resistance_w REAL, net_w REAL,
    PRIMARY KEY (date, ticker, snap_time))'''

_MORNING_DDL = '''CREATE TABLE IF NOT EXISTS morning (
    date TEXT, ticker TEXT, snap_time TEXT, spot REAL,
    wall_0 REAL, support_0 REAL, resistance_0 REAL, net_0 REAL,
    wall_w REAL, support_w REAL, resistance_w REAL, net_w REAL,
    PRIMARY KEY (date, ticker))'''


def save_snapshot(db_path, table, date_, ticker, snap_time, spot, lvl_0, lvl_w):
    """Write one GEX snapshot row. lvl_0 / lvl_w = (wall, supp, res, net)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    w0, s0, r0, n0 = lvl_0
    ww, sw, rw, nw = lvl_w
    ddl = _INTRADAY_DDL if table == 'intraday' else _MORNING_DDL
    with sqlite3.connect(db_path) as conn:
        conn.execute(ddl)
        conn.execute(f'INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            date_, ticker, snap_time, round(spot, 2),
            w0, s0, r0, round(n0 / 1e9, 3) if n0 else 0,
            ww, sw, rw, round(nw / 1e9, 3) if nw else 0,
        ))


def load_snapshots(db_path, table, date_, ticker=None):
    """Return all rows for date_ (optionally filtered by ticker) as list of dicts."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            if ticker:
                rows = conn.execute(
                    f'SELECT * FROM {table} WHERE date=? AND ticker=? ORDER BY snap_time',
                    (date_, ticker)
                ).fetchall()
            else:
                rows = conn.execute(
                    f'SELECT * FROM {table} WHERE date=? ORDER BY ticker, snap_time',
                    (date_,)
                ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
