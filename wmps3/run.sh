#!/usr/bin/with-contenv bash
set -Eeuo pipefail

# Activate venv
export PATH="/opt/venv/bin:${PATH}"

echo "[WMPS] Starting add-on..."

# Show sanitized options (hide token)
if [ -f /data/options.json ]; then
  echo "[WMPS] options.json (sanitized):"
  # ha_token değerini maskeler
  sed -E 's/("ha_token"\s*:\s*")[^"]+(")/\1***REDACTED***\2/g' /data/options.json | head -n 200
else
  echo "[WMPS] WARNING: /data/options.json not found"
fi

# App root
cd /opt/wmps

# Optional: evdev modunda cihaz erişimi kontrolü
if grep -q '"keypad_source"\s*:\s*"evdev"' /data/options.json 2>/dev/null; then
  if [ ! -r /dev/input ]; then
    echo "[WMPS] WARNING: keypad_source=evdev ancak /dev/input okunamıyor. Add-on manifestte devices mapping ve izinleri kontrol et."
  fi
fi

# Ingress arkasında doğru proxy başlıkları (IP/scheme) için forwarded allow ekle
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips="*"
