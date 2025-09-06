#!/usr/bin/env bash
set -euo pipefail

# --- Resolve project directory ---
# Allow this script to be run from anywhere; we will cd into the folder that contains app/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${SCRIPT_DIR}/app" ]]; then
  PROJ_DIR="${SCRIPT_DIR}"
elif [[ -d "${SCRIPT_DIR}/wmps/app" ]]; then
  PROJ_DIR="${SCRIPT_DIR}/wmps"
else
  echo "[WMPS] ERROR: Cannot find 'app/' folder next to this script (or under ./wmps)."
  exit 1
fi
cd "${PROJ_DIR}"

echo "[WMPS] Project dir: ${PROJ_DIR}"

# --- Optional: activate or create a local virtualenv ---
if [[ -d ".venv" ]]; then
  echo "[WMPS] Activating existing .venv"
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
else
  echo "[WMPS] No .venv found. You can create one with:"
  echo "       python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
fi

# --- Optional: install dependencies if inside venv ---
if [[ -n "${VIRTUAL_ENV:-}" && -f "requirements.txt" ]]; then
  echo "[WMPS] Installing Python dependencies from requirements.txt (if needed)"
  pip install --upgrade pip wheel setuptools >/dev/null 2>&1 || true
  pip install -r requirements.txt
fi

# --- Run FastAPI with auto reload ---
echo "[WMPS] Starting FastAPI (reload on code changes) at http://127.0.0.1:8000"
exec uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
