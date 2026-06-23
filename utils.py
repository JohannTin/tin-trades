import logging
import os
import time
from contextlib import contextmanager
from datetime import date

import pandas_market_calendars as mcal
import requests


def send_telegram(text):
    token   = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return
    requests.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        json={'chat_id': chat_id, 'text': f'<pre>{text}</pre>', 'parse_mode': 'HTML'},
        timeout=10,
    )


def setup_logger(name='tin-trades', prefix=None):
    os.makedirs('logs', exist_ok=True)
    slug = prefix or name.replace('-', '_')
    fmt  = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    fh = logging.FileHandler(f'logs/{slug}.log')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers = []
    logger.addHandler(fh)
    logger.addHandler(sh)

    return logger


@contextmanager
def log_run(logger, name):
    t0 = time.time()
    logger.info('=' * 50)
    logger.info(f'tin-trades {name} — {date.today()}')
    yield
    logger.info(f'Done in {time.time()-t0:.1f}s')
    logger.info('=' * 50)


def is_market_open(check_date=None):
    d = str(check_date or date.today())
    nyse = mcal.get_calendar('NYSE')
    return not nyse.schedule(start_date=d, end_date=d).empty
