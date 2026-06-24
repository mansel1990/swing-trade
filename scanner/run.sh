#!/usr/bin/env bash
# DigitalOcean evening scan — crontab: 0 18 * * 1-5 .../scanner/run.sh
#
# Runs the self-contained swing scanner (main_sanjay.py → swing.* tables).
# ML heroes (TeCNa, Wayuputra, …) are written by daily_suggestor on your
# LOCAL machine → daily_suggestor.trades in Neon. This script does NOT run those.
set -euo pipefail

SCANNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${SCANNER_DIR}/venv"
LOG="/var/log/mcube-scanner.log"

if [[ ! -x "${VENV}/bin/python" ]]; then
  echo "[$(date -Is)] ERROR: venv missing at ${VENV}" >> "${LOG}"
  exit 1
fi

{
  echo "=== $(date -Is) swing-trade server scan ==="
  cd "${SCANNER_DIR}"
  set -a && source .env && set +a
  "${VENV}/bin/pip" install -q -r requirements.txt
  "${VENV}/bin/python" main_sanjay.py --save
} >> "${LOG}" 2>&1
