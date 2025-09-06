#!/usr/bin/env bash
set -euo pipefail

# -------------------------------
# WMPS dev runner (uvicorn)
# - Works from anywhere; finds project dir with app/
# - Optional .venv activation and requirements install
# - Optional .env loading
# - CLI flags: --reload/--no-reload, --proxy, --host=, --port=, --app=, --workers=, --log-level=, --env=
# -------------------------------

# --- Colors (optional pretty logs) ---
if [[ -t 1 ]]; then
  cB=$'\033[1m'; cG=$'\033[32m'; cY=$'\033[33m'; cR=$'\033[31m'; c0=$'\033[0m'
else
  cB=""; cG=""; cY=""; cR=""; c0=""
fi

# --- Usage ---
usage() {
  cat <<'USAGE'
Usage: dev.sh [options]

Options:
  --reload            Enable auto-reload (default)
  --no-reload         Disable auto-reload
  --proxy             Enable --proxy-headers (for reverse proxies/Ingress)
  --no-proxy          Disable --proxy-headers (default)
  --host=HOST         Bind host (default: 0.0.0.0)
  --port=PORT         Bind port (default: 8000)
  --app=IMPORT        ASGI app import path (default: app.main:app)
  --workers=N         Run with N workers (disables --reload)
  --log-level=LEVEL   Uvicorn log level (info, debug, warning, error)
  --env=FILE          Load environment variables from FILE (default: .env if exists)
  --venv              Require .venv (exit if missing)
  -h|--help           Show this help

Examples:
  ./dev.sh --reload --proxy --host=127.0.0.1 --port=8000
  ./dev.sh --workers=2 --log-level=info
USAGE
}

# --- Resolve project directory (must contain app/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${SCRIPT_DIR}/app" ]]; then
  PROJ_DIR="${SCRIPT_DIR}"
elif [[ -d "${SCRIPT_DIR}/wmps/app" ]]; then
  PROJ_DIR="${SCRIPT_DIR}/wmps"
elif [[ -d "${SCRIPT_DIR}/../app" ]]; then
  PROJ_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  echo "${cR}[WMPS] ERROR:${c0} Cannot find 'app/' folder near this script."
  exit 1
fi
cd "${PROJ_DIR}"
echo "${cB}[WMPS] Project dir:${c0} ${PROJ_DIR}"

# --- Defaults ---
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
APP="${APP:-app.main:app}"
RELOAD=1
PROXY_HEADERS=0
WORKERS=""
LOG_LEVEL="${LOG_LEVEL:-}"
ENV_FILE=""
FORCE_VENV=0

# --- Parse args ---
for arg in "$@"; do
  case "${arg}" in
    --reload) RELOAD=1;;
    --no-reload) RELOAD=0;;
    --proxy) PROXY_HEADERS=1;;
    --no-proxy) PROXY_HEADERS=0;;
    --host=*) HOST="${arg#*=}";;
    --port=*) PORT="${arg#*=}";;
    --app=*) APP="${arg#*=}";;
    --workers=*) WORKERS="${arg#*=}"; RELOAD=0;;
    --log-level=*) LOG_LEVEL="${arg#*=}";;
    --env=*) ENV_FILE="${arg#*=}";;
    --venv) FORCE_VENV=1;;
    -h|--help) usage; exit 0;;
    *) echo "${cY}[WMPS] WARN:${c0} Unknown option '${arg}'"; usage; exit 1;;
  esac
done

# --- Load .env if requested or present ---
if [[ -z "${ENV_FILE}" && -f ".env" ]]; then
  ENV_FILE=".env"
fi
if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
  echo "${cB}[WMPS] Loading env:${c0} ${ENV_FILE}"
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# --- Activate or guide for .venv ---
if [[ -d ".venv" ]]; then
  echo "${cB}[WMPS] Activating .venv${c0}"
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
elif [[ "${FORCE_VENV}" -eq 1 ]]; then
  echo "${cR}[WMPS] ERROR:${c0} .venv is required (use --venv)."
  echo "       Create it with:"
  echo "       python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
else
  echo "${cY}[WMPS] No .venv found.${c0} You can create one with:"
  echo "       python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
fi

# --- Optional: install dependencies if inside venv ---
if [[ -n "${VIRTUAL_ENV:-}" && -f "requirements.txt" ]]; then
  echo "${cB}[WMPS] Ensuring Python deps (requirements.txt)${c0}"
  python -m pip install --upgrade pip wheel setuptools >/dev/null 2>&1 || true
  python -m pip install -r requirements.txt
fi

# --- Ensure Python path points to project root ---
export PYTHONPATH="${PROJ_DIR}:${PYTHONPATH:-}"

# --- Basic sanity checks ---
if ! python -c "import importlib; import sys; import uvicorn" >/dev/null 2>&1; then
  echo "${cR}[WMPS] ERROR:${c0} 'uvicorn' is not installed in the current environment."
  echo "       Try: pip install uvicorn[standard] fastapi"
  exit 1
fi
if ! python -c "import importlib; import sys; import importlib.util as u; sys.exit(0 if u.find_spec('${APP%%:*}') else 1)" >/dev/null 2>&1; then
  echo "${cR}[WMPS] ERROR:${c0} Cannot import module for app '${APP}'."
  echo "       Ensure your app path is correct, e.g. --app=app.main:app"
  exit 1
fi

# --- Build uvicorn args ---
UV_ARGS=( "${APP}" "--host" "${HOST}" "--port" "${PORT}" )
if [[ "${RELOAD}" -eq 1 ]]; then
  UV_ARGS+=( "--reload" )
fi
if [[ -n "${WORKERS}" ]]; then
  UV_ARGS+=( "--workers" "${WORKERS}" )
fi
if [[ "${PROXY_HEADERS}" -eq 1 ]]; then
  UV_ARGS+=( "--proxy-headers" "--forwarded-allow-ips=*" )
fi
if [[ -n "${LOG_LEVEL}" ]]; then
  UV_ARGS+=( "--log-level" "${LOG_LEVEL}" )
fi

echo "${cG}[WMPS] Starting FastAPI at http://${HOST}:${PORT}${c0}"
echo "${cB}[WMPS] App:${c0} ${APP}  ${cB}Reload:${c0} $([[ ${RELOAD} -eq 1 ]] && echo on || echo off)  ${cB}Proxy:${c0} $([[ ${PROXY_HEADERS} -eq 1 ]] && echo on || echo off)"

# --- Exec uvicorn ---
exec python -m uvicorn "${UV_ARGS[@]}"
