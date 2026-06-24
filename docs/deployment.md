# Deployment — two pipelines, one database (Neon)

## Architecture (correct split)

| Where | What runs | Writes to | Powers in UI |
|---|---|---|---|
| **Your local PC** | `daily_suggestor` (separate project, not in this repo) | `daily_suggestor.trades`, `daily_suggestor.daily_runs` | ML heroes — TeCNa, Wayuputra, Gobin xood, etc. |
| **DigitalOcean droplet** | `scanner/main_sanjay.py --save` via `run.sh` | `swing.*_signals`, `swing.strategy_performance` | Benched swing strategies (EMA, RS, breakout, …) |

The mcube app reads **both** from Neon. They are independent cron jobs.

**Nothing from daily_suggestor was merged into swing-trade.** This repo contains:
- `main_sanjay.py` — full self-contained 7-strategy scanner (server)
- `main.py` — thin wrapper that imports external `daily_suggestor/run_daily.py` (local only)

Jun 11 broke the droplet because cron was switched to `main.py`, which needs `daily_suggestor` on the server — that was never the design.

---

## DigitalOcean (Bangalore droplet)

```
/opt/mcube-scanner/swing-trade/scanner/
├── main_sanjay.py    ← server runs this
├── run.sh            ← cron entrypoint
├── venv/
└── .env              ← DATABASE_URL
```

Crontab (already correct):

```
0 18 * * 1-5 /opt/mcube-scanner/swing-trade/scanner/run.sh
```

### Fix / verify on droplet

```bash
cd /opt/mcube-scanner/swing-trade
git pull origin master

cd scanner
source venv/bin/activate
pip install -r requirements.txt

# Update run.sh to use main_sanjay (after git pull), or patch manually:
# python main_sanjay.py --save  instead of  python main.py

chmod +x run.sh
./run.sh
tail -50 /var/log/mcube-scanner.log
```

Success: `[Performance tracker]`, `Saved N signal(s) to swing.ema_signals`, etc. **No** `run_daily` import.

---

## Local machine (ML picks)

Run your `daily_suggestor` project each evening after market close (Task Scheduler / manual):

```bash
cd path/to/daily_suggestor
python run_daily.py
```

Or, if you have both repos as siblings:

```bash
cd path/to/swing-trade/scanner
DAILY_SUGGESTOR_DIR=../../daily_suggestor python main.py
```

Verify ML writes:

```sql
SELECT run_date, new_buys, closes, ran_at
FROM daily_suggestor.daily_runs
ORDER BY run_date DESC LIMIT 3;
```

---

## Railway — retired

Delete any old Railway `swing-trade` project in the Railway dashboard.

## GitHub Actions backup

`.github/workflows/daily-scan.yml` SSHs to DO and runs `run.sh` (main_sanjay).

Secrets: `DO_HOST`, `DO_USER`, `DO_SSH_KEY`.
