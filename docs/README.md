# Swing Trading Scanner — Documentation Index

## Project Overview
Python momentum scanner for NSE stocks. Runs 5 strategies each evening, saves signals to Neon Postgres, deployed on Railway with a daily cron.

## Docs

| File | Contents |
|---|---|
| [architecture.md](architecture.md) | System design, data flow, how strategies plug in |
| [strategies.md](strategies.md) | All 5 strategies — filters, logic, parameters |
| [database.md](database.md) | All DB tables, schemas, how to add a new table |
| [deployment.md](deployment.md) | Railway setup, cron schedule, env vars |
| [adding-a-strategy.md](adding-a-strategy.md) | Step-by-step guide to add a 6th strategy |

## Quick Commands
```bash
# Scan only (no DB write)
python main.py

# Scan + save all strategies
python main.py --save

# Run one strategy only
python main.py --strategy ema   # ema | breakout | vcp | rs | mr

# Run just one strategy and save
python main.py --strategy rs --save
```
