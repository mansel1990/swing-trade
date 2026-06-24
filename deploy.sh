#!/usr/bin/env bash
# DigitalOcean deploy — /opt/mcube-scanner/swing-trade/deploy.sh
set -euo pipefail

BASE="${DEPLOY_BASE:-/opt/mcube-scanner}"
REPO="${DEPLOY_REPO:-${BASE}/swing-trade}"
SCANNER="${REPO}/scanner"
VENV="${SCANNER}/venv"
LOG="/var/log/mcube-scanner.log"
CRON_MARK="# mcube-scanner-evening"

echo "[deploy] repo=${REPO}"

cd "${REPO}"
git pull origin master

if [[ ! -d "${VENV}" ]]; then
  python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install -q -r "${SCANNER}/requirements.txt"

chmod +x "${SCANNER}/run.sh"
touch "${LOG}"

CRON_LINE="0 18 * * 1-5 ${SCANNER}/run.sh ${CRON_MARK}"
( crontab -l 2>/dev/null | grep -v "${CRON_MARK}" || true; echo "${CRON_LINE}" ) | crontab -

echo "[deploy] done — runs main_sanjay.py (swing bench), NOT ML daily_suggestor"
echo "[deploy] test: ${SCANNER}/run.sh && tail ${LOG}"
