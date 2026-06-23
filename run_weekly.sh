#!/bin/bash
cd /Users/johanntin/Documents/GitHub/tin-trades
.venv/bin/python earnings.py --next >> logs/cron.log 2>&1
.venv/bin/python events.py --next >> logs/cron.log 2>&1
