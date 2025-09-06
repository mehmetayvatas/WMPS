"""
Utility functions and centralized logger for WMPS.
- Robust log directory detection with fallbacks (HA add-on friendly).
- Rotating file logs + console logs.
- Secret redaction for tokens (JWT/LLAT/ha_token) in all outputs.
- UTC timestamps.
"""

from __future__ import annotations

import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler
from typing import Optional


# ---------- Redaction ----------

_JWT_RE = re.compile(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_LLAT_RE = re.compile(r"\bLLAT_[A-Za-z0-9_-]+\b")
_HA_TOKEN_JSON_RE = re.compile(r'("ha_token"\s*:\s*")[^"]+(")', re.IGNORECASE)

def _redact(text: str) -> str:
    """Redact likely secrets from log lines."""
    if not text:
        return text
    red = _JWT_RE.sub("***REDACTED_JWT***", text)
    red = _LLAT_RE.sub("***REDACTED_LLAT***", red)
    red = _HA_TOKEN_JSON_RE.sub(r'\1***REDACTED***\2', red)
    return red


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts secrets after standard formatting."""
    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        return _redact(s)


# ---------- Log dir resolution ----------

def _ensure_dir(path: str) -> Optional[str]:
    try:
        os.makedirs(path, exist_ok=True)
        # quick writability check
        test_file = os.path.join(path, ".wmps_touch")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_file)
        return path
    except Exception:
        return None


def _resolve_log_dir() -> str:
    # Priority: env override → /config → /share → /data → /tmp
    candidates = [
        os.environ.get("WMPS_LOG_DIR"),
        "/config/WMPS/logs",
        "/share/wmps/logs",
        "/data/logs",
        "/tmp",
    ]
    for c in candidates:
        if c and _ensure_dir(c):
            return c
    # last fallback
    return "/tmp"


LOG_DIR = _resolve_log_dir()
LOG_FILE = os.path.join(LOG_DIR, "wmps.log")


# ---------- Logger setup ----------

# UTC timestamps
logging.Formatter.converter = time.gmtime

_LOG_FORMAT = "%(asctime)s [%(levelname)s] [WMPS] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

logger = logging.getLogger("WMPS")
logger.setLevel(os.environ.get("WMPS_LOG_LEVEL", "INFO").upper())

# Avoid duplicate handlers if module is re-imported
if logger.handlers:
    for h in list(logger.handlers):
        logger.removeHandler(h)

# Console handler
_console = logging.StreamHandler()
_console.setLevel(logger.level)
_console.setFormatter(RedactingFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
logger.addHandler(_console)

# Rotating file handler (best-effort)
try:
    _file = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5)
    _file.setLevel(logger.level)
    _file.setFormatter(RedactingFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(_file)
    logger.info(f"[LOG] File logging to {LOG_FILE}")
except Exception as e:
    logger.warning(f"[LOG] File handler unavailable ({e}); console-only mode")


def get_logger() -> logging.Logger:
    """Return the WMPS logger."""
    return logger


def set_log_level(level: str = "INFO") -> None:
    """Dynamically change WMPS log level."""
    lvl = getattr(logging, (level or "INFO").upper(), logging.INFO)
    logger.setLevel(lvl)
    for h in logger.handlers:
        h.setLevel(lvl)
