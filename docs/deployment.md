# Deployment — Railway

## Platform
- **Service:** Railway (https://railway.app)
- **Repo:** https://github.com/mansel1990/swing-trade
- **Root directory in Railway settings:** `scanner/` (critical — Railway must know it's Python)
- **Runtime:** Python (detected from requirements.txt in scanner/)

## Cron schedule
Defined in `scanner/railway.toml`:
```toml
[deploy]
cronSchedule = "30 12 * * 1-5"
```
This is **12:30 UTC = 6:00 PM IST**, Monday–Friday. The scanner runs automatically after market close each weekday.

## Environment variables (set in Railway dashboard)
| Var | Description |
|---|---|
| DATABASE_URL | Neon Postgres connection string |
| DB_SCHEMA | `swing` |

## How to redeploy
1. Push to `main` branch → Railway auto-deploys
2. Or trigger manually from Railway dashboard → "Deploy"

## Cron execution
Railway runs `python main.py --save` (the start command). Check Railway logs to confirm signals were saved after each cron run.

## Monitoring
- Railway dashboard → Deployments → click a cron run → view logs
- Neon console → Tables → check `swing.ema_signals` etc. for today's date

## requirements.txt (scanner/)
```
yfinance
pandas
numpy
psycopg2-binary
python-dotenv
```
