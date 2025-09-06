#!/usr/bin/with-contenv bash
set -e

# Activate venv
export PATH="/opt/venv/bin:${PATH}"

# Debug banner
echo "[WMPS] Starting add-on..."
if [ -f /data/options.json ]; then
  echo "[WMPS] options.json:"
  cat /data/options.json || true
else
  echo "[WMPS] WARNING: /data/options.json not found"
fi

# App root
cd /opt/wmps

# Important: proxy headers are required behind Home Assistant Ingress
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
