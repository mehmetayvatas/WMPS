# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
import os
import time
import threading
import urllib.request, urllib.error
import fcntl
import glob
import shutil
import socket
import ssl
import traceback


from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union, List, Tuple

# Optional imports (guarded)
try:
    from evdev import InputDevice, categorize, ecodes  # evdev mode
    _HAS_EVDEV = True
except Exception:
    _HAS_EVDEV = False

try:
    from pymodbus.client import ModbusTcpClient  # ADAM-6050
    _HAS_PYMODBUS = True
except Exception:
    _HAS_PYMODBUS = False

try:
    import websocket  # HA WebSocket events (websocket-client)
    _HAS_WS = True
except Exception:
    _HAS_WS = False

from fastapi import FastAPI, Query, HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, HTMLResponse, StreamingResponse

# ----------------------- PATHS ---------------------------
DATA_DIR = Path("/data")
SHARE_DIR = Path("/share/wmps")
LOCK_DIR = DATA_DIR / "locks"
LOCK_DIR.mkdir(parents=True, exist_ok=True)
GLOBAL_LOCK = DATA_DIR / ".wmps.lock"

ACCOUNTS_PATH = DATA_DIR / "accounts.csv"
TRANSACTIONS_PATH = DATA_DIR / "transactions.csv"
OPTIONS_PATH = DATA_DIR / "options.json"

SHARE_ACCOUNTS = SHARE_DIR / "accounts.csv"
SHARE_TX = SHARE_DIR / "transactions.csv"

ACCOUNTS_HEADER = "tenant_code,name,balance,last_transaction_utc\n"
TRANSACTIONS_HEADER = "timestamp,tenant_code,machine_number,amount_charged,balance_before,balance_after,cycle_minutes,success\n"

# ADAM-6050 mapping: DO0..DO5 are Modbus coils 16..21
ADAM_COIL_BASE = 16

ACTIVE_UNTIL: Dict[str, float] = {}

app = FastAPI(title="WMPS API", version="3.2.0")

# ----------------------- Logging -------------------------

def _resolve_token(cfg_token: Optional[str]) -> Optional[str]:
    t = (cfg_token or "").strip()
    if t:
        return t
    return os.environ.get("SUPERVISOR_TOKEN")




# Prefer centralized logger if available
def _get_logger():
    try:
        from app.utils import get_logger  # type: ignore
        return get_logger()
    except Exception:
        return None

_LOGGER = _get_logger()

_LEVEL_MAP = {
    "DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARNING", "WARNING": "WARNING", "ERROR": "ERROR"
}

def _log(level: str, msg: str) -> None:
    """Log via app.utils logger if present, else print."""
    if _LOGGER:
        try:
            import logging as _logging  # local import to map level
            lvl = getattr(_logging, _LEVEL_MAP.get(level.upper(), "INFO"))
            _LOGGER.log(lvl, msg)
            return
        except Exception:
            pass
    print(f"[{datetime.now(timezone.utc).isoformat()}] {level}: {msg}", flush=True)

# ----------------------- Options / Config ----------------
def _read_options() -> dict:
    if not OPTIONS_PATH.exists():
        return {}
    try:
        return json.loads(OPTIONS_PATH.read_text(encoding="utf-8") or "{}")
    except Exception as e:
        _log("WARN", f"Failed to read options.json: {e}")
        return {}

def _write_options(opts: dict) -> None:
    tmp = OPTIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(opts, indent=2), encoding="utf-8")
    os.replace(tmp, OPTIONS_PATH)
    _log("INFO", "options.json updated")

def _ensure_file(path: Path, header: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with path.open("w", encoding="utf-8", newline="") as f:
                f.write(header)
                f.flush(); os.fsync(f.fileno())
            _log("INFO", f"Created {path} with header")
    except Exception as e:
        _log("WARN", f"ensure_file failed for {path}: {e}")

def _mirror(src: Path, dst: Path):
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        with dst.open("wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        _log("INFO", f"Mirrored {src} -> {dst} ({len(data)} bytes)")
    except Exception as e:
        _log("WARN", f"Mirror failed {src} -> {dst}: {e}")

def ensure_bootstrap_files() -> None:
    _ensure_file(ACCOUNTS_PATH, ACCOUNTS_HEADER)
    _ensure_file(TRANSACTIONS_PATH, TRANSACTIONS_HEADER)
    _mirror(ACCOUNTS_PATH, SHARE_ACCOUNTS)
    _mirror(TRANSACTIONS_PATH, SHARE_TX)

    # options bootstrap (idempotent)
    if not OPTIONS_PATH.exists():
        default = {
            "ha_url": "http://supervisor/core",
            "ha_token": "",
            "simulate": True,
            "tts_service": "tts.google_translate_say",
            "media_player": "media_player.vlc_telnet",

            # keypad
            "keypad_source": "ha",  # ha|evdev|auto
            "ha_ws_url": None,      # derive from ha_url if None
            "ha_event_type": "keyboard_remote_command_received",

            # ADAM-6050 defaults
            "adam_host": "192.168.1.101",
            "adam_port": 502,
            "adam_unit_id": 1,
            "do_mode": "pulse",            # pulse|hold
            "pulse_seconds": 0.8,
            "invert_di": False,
            "activation_confirm_timeout_s": 15,

            "washing_machines": [1,2,3],
            "dryer_machines": [4,5,6],
            "washing_minutes": 30,
            "dryer_minutes": 60,
            "price_washing": 5.0,
            "price_dryer": 5.0,
            "price_map": {"1":5,"2":5,"3":5,"4":5,"5":5,"6":5},
            "disabled_machines": [],
            "machines": [
                {"id":1, "ha_switch":"switch.washer_1_control", "ha_sensor":"binary_sensor.washer_1_status", "relay":0, "di":0, "enabled": True},
                {"id":2, "ha_switch":"switch.washer_2_control", "ha_sensor":"binary_sensor.washer_2_status", "relay":1, "di":1, "enabled": True},
                {"id":3, "ha_switch":"switch.washer_3_control", "ha_sensor":"binary_sensor.washer_3_status", "relay":2, "di":2, "enabled": True},
                {"id":4, "ha_switch":"switch.dryer_4_control",  "ha_sensor":"binary_sensor.dryer_4_status",  "relay":3, "di":3, "enabled": True},
                {"id":5, "ha_switch":"switch.dryer_5_control",  "ha_sensor":"binary_sensor.dryer_5_status",  "relay":4, "di":4, "enabled": True},
                {"id":6, "ha_switch":"switch.dryer_6_control",  "ha_sensor":"binary_sensor.dryer_6_status",  "relay":5, "di":5, "enabled": True}
            ]
        }
        OPTIONS_PATH.write_text(json.dumps(default, indent=2), encoding="utf-8")
        _log("INFO", "Created /data/options.json with defaults")

# ----------------------- Locks ---------------------------
@contextmanager
def file_lock(path: Path, timeout: float = 10.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    start = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() - start > timeout:
                    raise TimeoutError(f"Could not acquire lock {path} within {timeout}s")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)

# ----------------------- Helpers -------------------------
def _ha_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def _ha_call_service(ha_url: str, token: str, domain: str, service: str, payload: dict) -> dict:
    url = f"{ha_url.rstrip('/')}/api/services/{domain}/{service}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_ha_headers(token), method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return {"raw": body.decode("utf-8","ignore")}

def _ha_get_state(ha_url: str, token: str, entity_id: str) -> dict:
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    req = urllib.request.Request(url, headers=_ha_headers(token), method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except Exception:
            return {"raw": body.decode("utf-8","ignore")}

def _num_to_text(v) -> str:
    if v is None or v == "":
        return ""
    try:
        fv = float(str(v).replace(",", "."))
        if abs(fv - int(fv)) < 1e-9:
            return str(int(fv))
        txt = ("%.2f" % fv)
        return txt.rstrip("0").rstrip(".") if "." in txt else txt
    except Exception:
        return str(v)

def _machine_category(machine: str, opts: dict) -> str:
    washing = [str(x) for x in (opts.get("washing_machines") or [])]
    dryer = [str(x) for x in (opts.get("dryer_machines") or [])]
    if str(machine) in washing:
        return "washing"
    if str(machine) in dryer:
        return "dryer"
    return "washing" if str(machine) in {"1","2","3"} else "dryer"

def _default_minutes_for(machine: str, opts: dict) -> int:
    return int(opts.get("washing_minutes", 30)) if _machine_category(machine, opts)=="washing" else int(opts.get("dryer_minutes", 60))

def _price_for(machine: str, opts: dict) -> float:
    pm = opts.get("price_map") or {}
    if str(machine) in pm:
        try: return float(pm[str(machine)])
        except: pass
    if isinstance(opts.get("price_per_cycle"), (int,float,str)):
        try: return float(opts.get("price_per_cycle"))
        except: pass
    return float(opts.get("price_washing", 5)) if _machine_category(machine, opts)=="washing" else float(opts.get("price_dryer", 5))

def _machine_entities(machine_id: str, opts: dict) -> Tuple[str, str]:
    for m in opts.get("machines", []) or []:
        if str(m.get("id")) == str(machine_id):
            return (m.get("ha_switch") or f"switch.machine_{machine_id}",
                    m.get("ha_sensor") or f"binary_sensor.machine_{machine_id}_busy")
    return (f"switch.machine_{machine_id}", f"binary_sensor.machine_{machine_id}_busy")

def _machine_adam_mapping(machine_id: str, opts: dict) -> Tuple[Optional[int], Optional[int]]:
    """Return (relay_index, di_index) for ADAM; defaults to id-1 if not provided."""
    for m in opts.get("machines", []) or []:
        if str(m.get("id")) == str(machine_id):
            r = m.get("relay")
            di = m.get("di")
            try_r = int(r) if r is not None else int(machine_id) - 1
            try_di = int(di) if di is not None else int(machine_id) - 1
            return try_r, try_di
    try:
        mid = int(machine_id)
        return mid - 1, mid - 1
    except Exception:
        return None, None

# ----------------------- CSV I/O -------------------------
def read_accounts() -> Dict[str, Dict[str, Union[str, float]]]:
    accounts: Dict[str, Dict[str, Union[str, float]]] = {}
    if not ACCOUNTS_PATH.exists():
        return accounts
    with ACCOUNTS_PATH.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if not row: continue
            tenant = (row.get("tenant_code") or row.get("customer_id") or "").strip()
            if not tenant: continue
            name = (row.get("name") or "").strip()
            bal_raw = (row.get("balance") or row.get("amount") or "0").strip().replace(",", ".")
            try: bal = float(bal_raw)
            except Exception: bal = 0.0
            ltx = (row.get("last_transaction_utc") or "").strip()
            accounts[tenant] = {"name": name, "balance": bal, "last_transaction_utc": ltx}
    return accounts

def write_accounts(accounts: Dict[str, Dict[str, Union[str, float]]]) -> None:
    tmp = ACCOUNTS_PATH.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tenant_code", "name", "balance", "last_transaction_utc"])
        for tenant, rec in accounts.items():
            name = rec.get("name", "")
            bal = float(rec.get("balance", 0.0))
            ltx = rec.get("last_transaction_utc", "")
            w.writerow([tenant, name, _num_to_text(bal), ltx])
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, ACCOUNTS_PATH)
    _mirror(ACCOUNTS_PATH, SHARE_ACCOUNTS)

def append_transaction(
    tenant_code: str,
    machine_number: str,
    amount_charged: float,
    balance_before: float,
    balance_after: float,
    cycle_minutes: Optional[int],
    success: Union[bool, str, int]
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    success_txt = "True" if (success is True or str(success).lower() in {"true","1","ok","success","yes"}) else "False"
    row = [
        ts, str(tenant_code),
        str(int(machine_number)) if str(machine_number).isdigit() else str(machine_number),
        _num_to_text(amount_charged),
        _num_to_text(balance_before),
        _num_to_text(balance_after),
        str(cycle_minutes) if cycle_minutes is not None else "",
        success_txt,
    ]
    _ensure_file(TRANSACTIONS_PATH, TRANSACTIONS_HEADER)
    with TRANSACTIONS_PATH.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(row); f.flush(); os.fsync(f.fileno())
    _mirror(TRANSACTIONS_PATH, SHARE_TX)
    _log("INFO", f"TX appended: {row}")

def tail_transactions(n: int = 50) -> List[Dict[str,str]]:
    if not TRANSACTIONS_PATH.exists():
        return []
    lines = TRANSACTIONS_PATH.read_text(encoding="utf-8").splitlines()
    if len(lines) <= 1: return []
    header = lines[0].split(",")
    body = lines[1:][-n:]
    out = []
    for ln in body:
        parts = ln.split(",")
        if len(parts) < len(header): continue
        out.append({
            "timestamp": parts[0],
            "tenant_code": parts[1],
            "machine_number": parts[2],
            "amount_charged": parts[3],
            "balance_after": parts[5],
            "cycle_minutes": parts[6] if len(parts) > 6 else "",
            "success": parts[7] if len(parts) > 7 else ""
        })
    return out

# ----------------------- TTS -----------------------------
def speak(text: str):
    """Use HA tts.speak with the new schema; fall back to legacy service if needed."""
    if not text:
        return
    opts = _read_options()
    url = opts.get("ha_url") or "http://supervisor/core"
    token = _resolve_token(opts.get("ha_token"))
    media_player = (opts.get("media_player") or "").strip()
    language = (opts.get("tts_language") or "").strip() or None
    if not token or not media_player:
        return

    # New schema (HA 2024+): entity_id is the MEDIA PLAYER.
    try:
        payload = {"entity_id": media_player, "message": text, "cache": False}
        if language:
            payload["language"] = language
        _ha_call_service(url, token, "tts", "speak", payload)
        _log("INFO", f"TTS speak OK -> {media_player}")
        return
    except Exception as e:
        _log("WARN", f"tts.speak failed: {e}")

    # Fallback to explicit service (e.g. google_translate_say)
    try:
        raw = (opts.get("tts_service") or "google_translate_say").strip()
        if raw.startswith("tts."):
            raw = raw.split(".", 1)[1]
        payload = {"entity_id": media_player, "message": text}
        if language:
            payload["language"] = language
        _ha_call_service(url, token, "tts", raw, payload)
        _log("INFO", f"TTS fallback tts.{raw} OK")
    except Exception as e:
        _log("WARN", f"TTS fallback failed: {e}")





# ----------------------- HA State/Availability -----------
def _get_state(entity_id: str, opts: dict) -> str:
    token = _resolve_token(opts.get("ha_token"))
    url = opts.get("ha_url") or "http://supervisor/core"
    if not token:
        return "simulated" if bool(opts.get("simulate", False)) else "unknown"
    try:
        st = _ha_get_state(url, token, entity_id)
        return st.get("state", "unknown")
    except Exception as e:
        _log("WARN", f"state read failed for {entity_id}: {e}")
        return "unknown"

# ----------------------- ADAM-6050 I/O -------------------
def _adam_cfg(opts: dict) -> dict:
    return {
        "host": opts.get("adam_host"),
        "port": int(opts.get("adam_port") or 502),
        "unit": int(opts.get("adam_unit_id") or 1),
        "do_mode": (opts.get("do_mode") or "pulse"),
        "pulse_seconds": float(opts.get("pulse_seconds") or 0.8),
        "invert_di": bool(opts.get("invert_di", False)),
    }

def _adam_read_di(di_index: int, opts: dict) -> Optional[bool]:
    """Return True if DI is active; None on error. Uses invert_di if set."""
    if not _HAS_PYMODBUS:
        return None
    cfg = _adam_cfg(opts)
    host = (cfg["host"] or "").strip()
    if not host:
        return None
    try:
        with ModbusTcpClient(host=host, port=cfg["port"], timeout=2.0) as client:  # type: ignore
            if not client.connect():
                _log("WARN", "ADAM: connect failed for DI read")
                return None
            rr = client.read_discrete_inputs(address=int(di_index), count=1, unit=cfg["unit"])
            if not hasattr(rr, "isError") or rr.isError():
                _log("WARN", "ADAM: read_discrete_inputs error")
                return None
            bits = getattr(rr, "bits", [False])
            raw = bool(bits[0] if bits else False)
            return (not raw) if cfg["invert_di"] else raw
    except Exception as e:
        _log("WARN", f"ADAM: DI read exception: {e}")
        return None

def _adam_write_coil(coil_index: int, state: bool, opts: dict) -> bool:
    """
    Write ADAM DO coil. ADAM-6050 maps DO0..5 to coils 16..21, so we add ADAM_COIL_BASE.
    """
    if not _HAS_PYMODBUS:
        return False
    cfg = _adam_cfg(opts)
    host = (cfg["host"] or "").strip()
    if not host:
        return False
    try:
        addr = ADAM_COIL_BASE + int(coil_index)
        with ModbusTcpClient(host=host, port=cfg["port"], timeout=2.0) as client:  # type: ignore
            if not client.connect():
                _log("WARN", "ADAM: connect failed for write_coil")
                return False
            wr = client.write_coil(addr, bool(state), unit=cfg["unit"])
            if not hasattr(wr, "isError") or wr.isError():
                _log("WARN", f"ADAM: write_coil error at addr={addr}")
                return False
            return True
    except Exception as e:
        _log("WARN", f"ADAM: write_coil exception: {e}")
        return False

def _adam_pulse(coil_index: int, pulse_seconds: float, opts: dict) -> bool:
    """
    Pulse ADAM DO coil with base offset applied.
    """
    if not _adam_write_coil(coil_index, True, opts):
        return False
    time.sleep(max(0.05, float(pulse_seconds)))
    _adam_write_coil(coil_index, False, opts)
    return True

# ----------------------- Availability & Operation --------
def machine_enabled(mid: str, opts: dict) -> bool:
    """
    Enabled precedence:
      1) top-level flag machine_{N}_enabled
      2) per-machine 'enabled' in machines[]
      3) not listed in disabled_machines
    Default: True
    """
    def _is_false(v) -> bool:
        if isinstance(v, bool):
            return v is False
        return str(v).strip().lower() in {"false", "0", "no", "off"}

    try:
        i = int(str(mid).strip())
        key = f"machine_{i}_enabled"
        if key in opts and _is_false(opts.get(key)):
            return False
    except Exception:
        pass

    for m in (opts.get("machines") or []):
        if str(m.get("id")) == str(mid):
            if _is_false(m.get("enabled", True)):
                return False
            break

    dm = [str(x) for x in (opts.get("disabled_machines") or [])]
    if str(mid) in dm:
        return False

    return True

def machine_is_available(mid: str, opts: dict) -> bool:
    """Prefer ADAM DI if configured; else HA sensor; else simulated/unknown."""
    if not machine_enabled(mid, opts):
        return False

    # ADAM DI path
    r, di = _machine_adam_mapping(mid, opts)
    if ((opts.get("adam_host") or "").strip()) and di is not None:
        di_state = _adam_read_di(di, opts)
        if di_state is not None:
            # DI True => RUNNING/BUSY (active). Available when False.
            return not di_state

    # HA sensor path
    switch, sensor = _machine_entities(mid, opts)
    if bool(opts.get("simulate", False)) or not opts.get("ha_token"):
        return True
    state = _get_state(sensor, opts)
    # DI semantics: 'on' => BUSY, 'off' => AVAILABLE
    return state in ("off", "false", "0", "idle", "unknown")

def operate_machine(mid: str, minutes: Optional[int]) -> bool:
    """
    Returns True if activation succeeded (or simulate), False otherwise.
    - If ADAM is configured, drive DO (pulse/hold) and confirm via DI (if available).
    - Otherwise, call HA switch turn_on and confirm via HA sensor.
    """
    opts = _read_options()
    simulate = bool(opts.get("simulate", False))
    url = opts.get("ha_url") or "http://supervisor/core"
    token = _resolve_token(opts.get("ha_token"))
    switch, sensor = _machine_entities(mid, opts)
    r, di = _machine_adam_mapping(mid, opts)

    try:
        confirm_timeout = int(opts.get("activation_confirm_timeout_s", 8) or 8)
    except Exception:
        confirm_timeout = 8

    if simulate:
        _log("INFO", f"[SIMULATE] turn ON {switch} for {minutes} minutes")
        return True

    # ADAM path if available
    if ((opts.get("adam_host") or "").strip()) and r is not None:
        do_mode = (opts.get("do_mode") or "pulse").lower()
        try:
            pulse_seconds = float(opts.get("pulse_seconds") or 0.8)
        except Exception:
            pulse_seconds = 0.8

        ok = _adam_pulse(r, pulse_seconds, opts) if do_mode == "pulse" else _adam_write_coil(r, True, opts)
        if not ok:
            _log("WARN", f"ADAM: failed to activate relay for machine {mid}")
            return False

        # Confirm via DI if possible
        t0 = time.monotonic()
        confirmed = True  # assume success if no DI configured
        if di is not None:
            confirmed = False
            while time.monotonic() - t0 < confirm_timeout:
                di_state = _adam_read_di(di, opts)
                if di_state is True:  # True => BUSY/RUNNING
                    confirmed = True
                    break
                time.sleep(0.5)

        if not confirmed:
            _log("WARN", f"Activation not confirmed by ADAM DI index={di} for machine {mid}")
            if do_mode == "hold":
                _adam_write_coil(r, False, opts)
            return False

        # Schedule turn_off for hold mode
        if do_mode == "hold" and minutes and minutes > 0:
            def turn_off_later():
                try:
                    _log("INFO", f"ADAM: set_off relay index={r} after {minutes} minutes (m#{mid})")
                    _adam_write_coil(r, False, opts)
                except Exception as e:
                    _log("WARN", f"ADAM: failed to turn OFF relay for m#{mid}: {e}")
            t = threading.Timer(minutes * 60.0, turn_off_later)
            t.daemon = True
            t.start()
        return True

    # HA switch path
    if not token:
        _log("WARN", "ha_token missing; cannot control HA.")
        return False

    try:
        _log("INFO", f"Turning ON {switch}")
        _ha_call_service(url, token, "switch", "turn_on", {"entity_id": switch})
    except Exception as e:
        _log("WARN", f"Failed to turn ON {switch}: {e}")
        return False

    # Confirm activation via HA sensor
    t0 = time.monotonic()
    ok = False
    while time.monotonic() - t0 < confirm_timeout:
        st = _get_state(sensor, opts)
        if st in ("on", "true", "1", "running"):
            ok = True
            break
        time.sleep(0.5)

    if not ok:
        _log("WARN", f"Activation not confirmed by {sensor}")
        try:
            _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
        except Exception:
            pass
        return False

    if minutes and minutes > 0:
        def turn_off_later():
            try:
                _log("INFO", f"Turning OFF {switch} after {minutes} minutes")
                _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
            except Exception as e:
                _log("WARN", f"Failed to turn OFF {switch}: {e}")
        t = threading.Timer(minutes * 60.0, turn_off_later)
        t.daemon = True
        t.start()
    return True


def _handle_charge(tenant_code: str, machine: str, price: Optional[float], minutes: Optional[int], opts: dict) -> dict:
    """
    Core flow:
      - read options + accounts
      - validate machine enabled & availability
      - resolve price/minutes defaults
      - pre-charge checks (tenant exists, balance)
      - activate switch/ADAM, confirm
      - adjust balance, append transaction
      - TTS
    """
    ensure_bootstrap_files()

    tenant_code = str(tenant_code).strip()
    machine = str(machine).strip()
    if not tenant_code or not machine:
        raise HTTPException(status_code=400, detail="INVALID_INPUT")

    if not machine_enabled(machine, opts):
        speak(f"Machine {machine} is currently disabled.")
        raise HTTPException(status_code=423, detail="MACHINE_DISABLED")

    if not machine_is_available(machine, opts):
        speak(f"Machine {machine} is busy. Please choose another machine.")
        raise HTTPException(status_code=409, detail="MACHINE_BUSY")

    p = float(price) if price is not None else _price_for(machine, opts)
    m = int(minutes) if minutes is not None else _default_minutes_for(machine, opts)
    if p <= 0:
        raise HTTPException(status_code=400, detail="PRICE_NOT_DEFINED")

    # Pre-check balance under global lock
    with file_lock(GLOBAL_LOCK, timeout=10.0):
        accounts = read_accounts()
        if tenant_code not in accounts:
            raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")
        bal_before = float(accounts.get(tenant_code, {}).get("balance", 0.0))
        if bal_before < p:
            speak("Insufficient balance.")
            append_transaction(tenant_code, machine, 0.0, bal_before, bal_before, m, success=False)
            raise HTTPException(status_code=402, detail="INSUFFICIENT_FUNDS")

    # Per-machine lock, then operate
    with file_lock(_machine_lock_path(machine), timeout=10.0):
        if not machine_is_available(machine, opts):
            speak(f"Machine {machine} is busy. Please choose another machine.")
            raise HTTPException(status_code=409, detail="MACHINE_BUSY")

        activated = operate_machine(machine, m)
        if not activated:
            with file_lock(GLOBAL_LOCK, timeout=10.0):
                accounts = read_accounts()
                bal0 = float(accounts.get(tenant_code, {}).get("balance", 0.0))
                append_transaction(tenant_code, machine, 0.0, bal0, bal0, m, success=False)
            raise HTTPException(status_code=500, detail="ACTIVATION_FAILED")

        # Soft-busy timer + hold için auto-release ----
        duration_s = int(m or 0) * 60
        if duration_s > 0:
            ACTIVE_UNTIL[machine] = time.time() + duration_s

            mode = str(opts.get("do_mode") or "pulse").lower()
            if mode == "hold":
                def _auto_release():
                    try:
                        url = opts.get("ha_url") or "http://supervisor/core"
                        token = _resolve_token(opts.get("ha_token"))
                        switch, _sensor = _machine_entities(machine, opts)
                        if token and switch:
                            _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
                    except Exception:
                        pass
                threading.Timer(duration_s, _auto_release).start()

        with file_lock(GLOBAL_LOCK, timeout=10.0):
            accounts = read_accounts()
            if tenant_code not in accounts:
                # rollback attempt (best-effort)
                try:
                    url = opts.get("ha_url") or "http://supervisor/core"
                    token = _resolve_token(opts.get("ha_token"))
                    switch, _sensor = _machine_entities(machine, opts)
                    if token:
                        _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
                except Exception:
                    pass
                ACTIVE_UNTIL.pop(machine, None)
                raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")

            bal_before = float(accounts[tenant_code].get("balance", 0.0))
            if bal_before < p:
                speak("Insufficient balance.")
                try:
                    url = opts.get("ha_url") or "http://supervisor/core"
                    token = _resolve_token(opts.get("ha_token"))
                    switch, _sensor = _machine_entities(machine, opts)
                    if token:
                        _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
                except Exception:
                    pass
                append_transaction(tenant_code, machine, 0.0, bal_before, bal_before, m, success=False)
                ACTIVE_UNTIL.pop(machine, None)
                raise HTTPException(status_code=402, detail="INSUFFICIENT_FUNDS")

            bal_after = bal_before - p
            accounts[tenant_code]["balance"] = bal_after
            accounts[tenant_code]["last_transaction_utc"] = datetime.now(timezone.utc).isoformat()
            append_transaction(tenant_code, machine, p, bal_before, bal_after, m, success=True)
            write_accounts(accounts)

    speak(f"Machine {machine} started for {m} minutes.")
    return {
        "ok": True,
        "tenant_code": tenant_code,
        "balance_before": bal_before,
        "balance_after": bal_after,
        "charged": p,
        "machine": machine,
        "cycle_minutes": m,
        "message": "CHARGE_OK"
    }

# ----------------------- Keypad (EVDEV + HA WS) ----------
# Minimal state machine: 6-digit code, then machine 1..6, then confirm
class KeypadStateMachine:
    """
    Flow:
      IDLE -> ENTER_CODE (6 digits then 'ENTER') -> SELECT_MACHINE (1..6) -> CONFIRM ('ENTER')
      '*' cancels and returns to IDLE at any time.
      Timeouts via options.security.code_entry_timeout_s (or default 30s).
    """
    def __init__(self, opts_provider, speak_fn, handle_charge_fn):
        self.opts_provider = opts_provider
        self.speak = speak_fn
        self.handle_charge = handle_charge_fn
        self.state = "IDLE"
        self.buf_code = ""
        self.sel_machine = None
        self.last_input = time.monotonic()

    def _timeout_s(self) -> int:
        opts = self.opts_provider() or {}
        sec = ((opts.get("security") or {}).get("code_entry_timeout_s"))
        return int(sec or 30)

    def on_sym(self, sym: str) -> None:
        now = time.monotonic()
        if now - self.last_input > self._timeout_s() and self.state != "IDLE":
            self._reset()
            self.speak("Timeout. Please enter your 6 digit code.")
        self.last_input = now

        if sym == "CANCEL":
            self._reset()
            self.speak("Cancelled.")
            return

        if self.state == "IDLE":
            if sym.isdigit():
                self.state = "ENTER_CODE"
                self.buf_code = sym
                self.speak("Enter your 6 digit code.")
            return

        if self.state == "ENTER_CODE":
            if sym.isdigit():
                if len(self.buf_code) < 6:
                    self.buf_code += sym
                return
            if sym == "ENTER":
                if len(self.buf_code) == 6:
                    accounts = read_accounts()
                    if self.buf_code not in accounts:
                        self.speak("Invalid code.")
                        self._reset()
                        return
                    self.state = "SELECT_MACHINE"
                    self.speak("Code accepted. Please select machine one through six.")
                else:
                    self.speak("Code must be 6 digits.")
            return

        if self.state == "SELECT_MACHINE":
            if sym.isdigit():
                n = int(sym)
                if 1 <= n <= 6:
                    self.sel_machine = n
                    mins = _default_minutes_for(str(n), self.opts_provider())
                    self.speak(f"Machine {n} selected for {mins} minutes. Press enter to confirm.")
                    self.state = "CONFIRM"
            return

        if self.state == "CONFIRM":
            if sym == "ENTER":
                try:
                    self.handle_charge(
                        tenant_code=self.buf_code,
                        machine=str(self.sel_machine),
                        price=None,
                        minutes=None,
                        opts=self.opts_provider()
                    )
                    self.speak("Payment accepted. Starting the cycle.")
                except HTTPException as e:
                    msg = str(e.detail)
                    if e.status_code == 409:
                        msg = "Machine is busy."
                    elif e.status_code == 402:
                        msg = "Insufficient balance."
                    elif e.status_code == 423:
                        msg = "Machine disabled."
                    elif e.status_code == 404:
                        msg = "User not found."
                    self.speak(msg)
                except Exception:
                    self.speak("Operation failed.")
                finally:
                    self._reset()

    def _reset(self) -> None:
        self.state = "IDLE"
        self.buf_code = ""
        self.sel_machine = None
        self.last_input = time.monotonic()



# Key mapping helpers
_MAIN_ROW_NUM = {2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8", 10: "9", 11: "0"}  # KEY_1..KEY_0
_KP_NUM = {79: "1", 80: "2", 81: "3", 75: "4", 76: "5", 77: "6", 71: "7", 72: "8", 73: "9", 82: "0"}  # KP1..KP0

def _map_keycode_name(name: str) -> Optional[str]:
    
    if not name:
        return None
    k = name
    if k.startswith("KEY_"):
        k = k[4:]
    if k in ("ENTER", "KPENTER"):
        return "ENTER"
    
    if k in ("KPASTERISK", "ESC", "BACKSPACE", "DELETE", "DEL", "TAB", "INS"):
        return "CANCEL"
    if k in ("HASHTAG",):
        return "ENTER"
    if k.startswith("KP") and len(k) == 3 and k[2].isdigit():
        return k[2]
    if len(k) == 1 and k.isdigit():
        return k
    if len(k) == 2 and k[0] == "F" and k[1].isdigit():
        return None
    return None

def _map_keycode_int(code: int) -> Optional[str]:
    if code in _MAIN_ROW_NUM:
        return _MAIN_ROW_NUM[code]
    if code in _KP_NUM:
        return _KP_NUM[code]
    if code in (28, 96):  # Enter, KP_Enter
        return "ENTER"
    
    if code in (1, 14, 111, 15):  # Esc, Backspace, Delete, Tab -> cancel
        return "CANCEL"
    if code in (43,):  # '#' fallback -> ENTER
        return "ENTER"
    return None


# EVDEV thread
def _evdev_thread():
    """Listen a physical USB keypad via evdev with auto-reconnect and rich key mapping."""
    if not _HAS_EVDEV:
        _log("WARN", "evdev not available; keypad_source=evdev cannot start")
        return

    sm = KeypadStateMachine(opts_provider=_read_options, speak_fn=speak, handle_charge_fn=_handle_charge)

    while True:
        # Prefer explicitly configured device; otherwise scan /dev/input
        try:
            opts_local = _read_options()
        except Exception:
            opts_local = {}
        path = (opts_local.get("keypad_device") or "").strip()

        if not path:
            path = _find_keypad_device()
            if not path:
                _log("WARN", "No keypad input device found under /dev/input (will retry in 5s)")
                time.sleep(5.0)
                continue
            else:
                _log("INFO", f"Keypad(evdev) selected by scan: {path}")
        else:
            # If a by-id path is configured but missing, fall back to scan
            if not os.path.exists(path):
                _log("WARN", f"Configured keypad_device not found: {path} (falling back to scan)")
                path = _find_keypad_device()
                if not path:
                    time.sleep(5.0)
                    continue
                _log("INFO", f"Keypad(evdev) fallback by scan: {path}")
            else:
                _log("INFO", f"Keypad(evdev) using configured device: {path}")

        # Try opening the device
        try:
            dev = InputDevice(path)
            dev_name = getattr(dev, "name", "unknown")
            _log("INFO", f"Keypad(evdev) listening on {path} ({dev_name})")
        except Exception as e:
            _log("WARN", f"Failed to open input device {path}: {e} (retry in 5s)")
            time.sleep(5.0)
            continue

        try:
            for event in dev.read_loop():
                # EV_KEY only; value 1 == key down
                if event.type != ecodes.EV_KEY or getattr(event, "value", None) != 1:
                    continue

                # Try resolve by key name first (from evdev.categorize)
                sym = None
                name = ""
                try:
                    key = categorize(event)
                    name = key.keycode if isinstance(key.keycode, str) else (
                        key.keycode[0] if isinstance(key.keycode, (list, tuple)) and key.keycode else ""
                    )
                    sym = _map_keycode_name(name)
                except Exception:
                    name = ""

                # Fallback: resolve by numeric code (main row / numpad / enter / cancel)
                if not sym:
                    try:
                        sym = _map_keycode_int(int(event.code))
                    except Exception:
                        sym = None

                # Honor confirm_keys from options as extra ENTER codes
                if not sym:
                    try:
                        ckeys = set(int(x) for x in (opts_local.get("confirm_keys") or []))
                        if int(event.code) in ckeys:
                            sym = "ENTER"
                    except Exception:
                        pass

                # Debug trace
                _log("DEBUG", f"evdev keydown code={event.code} name={name or '-'} -> {sym or '-'}")

                # Dispatch to state machine
                if sym:
                    sm.on_sym(sym)

        except OSError as e:
            # Device may have been unplugged; loop to reopen
            _log("WARN", f"Keypad(evdev) device error: {e} (will retry)")
            try:
                dev.close()
            except Exception:
                pass
            time.sleep(2.0)
            continue
        except Exception as e:
            _log("WARN", f"Keypad(evdev) loop exception: {e}\n{traceback.format_exc()}")
            try:
                dev.close()
            except Exception:
                pass
            time.sleep(2.0)
            continue


def _find_keypad_device(patterns=("event*",)):
    try:
        # quick dump for debugging
        try:
            import os
            listing = []
            if os.path.isdir("/dev/input"):
                for name in sorted(os.listdir("/dev/input")):
                    try:
                        st = os.stat(f"/dev/input/{name}")
                        listing.append(f"{name} mode={oct(st.st_mode & 0o777)} uid={st.st_uid} gid={st.st_gid}")
                    except Exception as e:
                        listing.append(f"{name} stat_err={e}")
            _log("INFO", "EVDEV: /dev/input -> " + (" | ".join(listing) if listing else "empty"))
        except Exception:
            pass

        # also dump kernel view
        try:
            with open("/proc/bus/input/devices", "r", encoding="utf-8") as f:
                txt = f.read()
            _log("INFO", "EVDEV: /proc/bus/input/devices:\n" + txt)
        except Exception as e:
            _log("WARN", f"EVDEV: cannot read /proc/bus/input/devices: {e}")
    except Exception:
        pass

    for p in glob.glob("/dev/input/" + patterns[0]):
        try:
            dev = InputDevice(p)
            name = (dev.name or "").lower()
            _log("INFO", f"EVDEV: candidate {p} name='{dev.name}'")
            if any(k in name for k in ("keyboard", "keypad", "rapoo", "usb")):
                return dev.path
        except Exception as e:
            _log("WARN", f"EVDEV: open failed {p}: {e}")
            continue
    return None


# HA WebSocket thread
def _derive_ws_url(http_url: Optional[str]) -> Optional[str]:
    if not http_url:
        return None
    u = http_url.strip().rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://"):] + "/api/websocket"
    if u.startswith("http://"):
        return "ws://" + u[len("http://"):] + "/api/websocket"
    return u  # assume already ws(s)

def _ha_ws_thread():
    if not _HAS_WS:
        _log("WARN", "websocket-client not available; keypad_source=ha cannot start")
        return
    opts = _read_options()
    token = _resolve_token(opts.get("ha_token"))
    if not token:
        _log("WARN", "HA WS: missing ha_token")
        return
    ws_url = opts.get("ha_ws_url") or _derive_ws_url(opts.get("ha_url") or "http://supervisor/core")
    event_type = opts.get("ha_event_type") or "keyboard_remote_command_received"
    sm = KeypadStateMachine(opts_provider=_read_options, speak_fn=speak, handle_charge_fn=_handle_charge)

    try:
        ws = websocket.create_connection(ws_url, timeout=8)  # type: ignore
    except Exception as e:
        _log("WARN", f"HA WS: connection failed: {e}")
        return

    def _send(obj):
        try:
            ws.send(json.dumps(obj))
        except Exception:
            pass

    try:
        # Expect auth_required -> auth_ok
        _ = ws.recv()
        _send({"type": "auth", "access_token": token})
        _ = ws.recv()
        # Subscribe to events
        _send({"id": 1, "type": "subscribe_events", "event_type": event_type})
        _log("INFO", f"HA WS: subscribed to {event_type}")

        while True:
            raw = ws.recv()
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("type") != "event":
                continue
            data = ((msg.get("event") or {}).get("data") or {})
            sym = None
            if "key_code" in data:
                try:
                    sym = _map_keycode_int(int(data["key_code"]))
                except Exception:
                    sym = None
            if not sym and "key" in data and isinstance(data["key"], str):
                sym = _map_keycode_name(data["key"])
            if not sym and "key_name" in data and isinstance(data["key_name"], str):
                sym = _map_keycode_name(data["key_name"])
            if sym:
                sm.on_sym(sym)
    except Exception as e:
        _log("WARN", f"HA WS: loop error: {e}")
    finally:
        try:
            ws.close()
        except Exception:
            pass


def start_keypad_listener():
    opts = _read_options()
    source = (opts.get("keypad_source") or "ha").lower()
    if source == "evdev":
        t = threading.Thread(target=_evdev_thread, daemon=True)
        t.start()
    elif source == "ha":
        t = threading.Thread(target=_ha_ws_thread, daemon=True)
        t.start()
    else:  # auto
        if _HAS_EVDEV and _find_keypad_device():
            _log("INFO", "keypad_source=auto -> using evdev")
            t = threading.Thread(target=_evdev_thread, daemon=True)
            t.start()
        else:
            _log("INFO", "keypad_source=auto -> using ha websocket")
            t = threading.Thread(target=_ha_ws_thread, daemon=True)
            t.start()

# ----------------------- Models & API --------------------
@app.on_event("startup")
def on_start():
    ensure_bootstrap_files()
    _log("INFO", "WMPS API started.")
    start_keypad_listener()

@app.get("/ping")
def ping():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/debug/tts")
def debug_tts(msg: str = "Hello from WMPS"):
    """
    Quick TTS test endpoint.
    Example: /debug/tts?msg=Merhaba+Memo
    """
    try:
        speak(msg)
        return {"ok": True, "message": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/", response_class=HTMLResponse)
def root_ui():
    return HTMLResponse("""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>WMPS</title>
  <style>
  
:root{
  --bg:#0b1220; --surface:#0f172a; --card:#111a2e;
  --muted:#9fb2c6; --text:#eaf1f8; --line:#24324f;
  --ok:#22c55e; --warn:#f59e0b; --err:#ef4444;
  --accent:#3b82f6; --accent-2:#1e40af;
  --radius-xs:10px; --radius:14px;
  --shadow-1:0 10px 28px rgba(0,0,0,.28);
  --shadow-2:0 6px 14px rgba(0,0,0,.18);
  --ring:0 0 0 3px rgba(59,130,246,.35);
}

*{ box-sizing:border-box }
html,body{ height:100% }
body{
  margin:0; padding:24px;
  background:var(--bg); color:var(--text);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Arial;
  line-height:1.55;
}

h2{
  margin:4px auto 20px auto; max-width:1280px;
  font-weight:800; letter-spacing:.2px;
}

.grid{
  display:grid;
  grid-template-columns: 1.25fr 1fr;
  grid-auto-rows: auto;
  grid-auto-flow: row dense;
  gap:12px;
  align-items:start;
  max-width:1280px; margin:0 auto;
}

.grid > .card:nth-of-type(3){ grid-column:1; grid-row:1; }
.grid > .card:nth-of-type(4){ grid-column:2; grid-row:1 / span 3; }
.grid > .card:nth-of-type(5){ grid-column:1; grid-row:2; }
.grid > .card:nth-of-type(6){ grid-column:1; grid-row:3; }
.grid > .card:nth-of-type(1){ grid-column:1 / -1; grid-row:4; }
.grid > .card:nth-of-type(2){ grid-column:1 / -1; grid-row:5; }

@media (max-width:1024px){
  .grid{ grid-template-columns:1fr; }
  .grid > .card:nth-of-type(3){ grid-column:1; grid-row:1; }
  .grid > .card:nth-of-type(5){ grid-column:1; grid-row:2; }
  .grid > .card:nth-of-type(6){ grid-column:1; grid-row:3; }
  .grid > .card:nth-of-type(4){ grid-column:1; grid-row:4; }
  .grid > .card:nth-of-type(1){ grid-column:1; grid-row:5; }
  .grid > .card:nth-of-type(2){ grid-column:1; grid-row:6; }
}

.card{
  background:linear-gradient(180deg, rgba(20,29,50,.70), rgba(17,26,46,.92));
  backdrop-filter:blur(6px);
  border:1px solid var(--line);
  border-radius:var(--radius);
  padding:12px;
  box-shadow:var(--shadow-1);
  align-self:start;
}
.card h3{ margin:0 0 8px 0; font-size:18px; font-weight:700 }

label{ display:block; margin:6px 0 6px 2px; font-size:12px; color:var(--muted) }
input{
  width:100%; padding:10px 12px; border-radius:var(--radius-xs);
  border:1px solid var(--line); background:#0c1628; color:#eaf1f8;
  outline:none; transition: border-color .15s, box-shadow .15s, background .15s;
}
input::placeholder{ color:#7d94ad }
input:hover{ border-color:#35507b }
input:focus-visible{ border-color:var(--accent); box-shadow:var(--ring); background:#0d1a30 }

button{
  background:linear-gradient(180deg,var(--accent),var(--accent-2));
  color:#fff; border:0; border-radius:var(--radius-xs);
  padding:9px 12px; cursor:pointer;
  box-shadow:var(--shadow-2);
  transition: transform .06s, filter .15s, box-shadow .15s;
}
button:hover{ filter:brightness(1.05) }
button:active{ transform:translateY(1px) }
button:focus-visible{ box-shadow:var(--ring) }
button:disabled{ opacity:.6; cursor:not-allowed; filter:grayscale(.2) }
button.btn-secondary{ background:linear-gradient(180deg,#64748b,#334155) }
button.btn-ghost{ background:transparent; border:1px solid var(--line) }
button.btn-danger{ background:linear-gradient(180deg,#ef4444,#7f1d1d) }

.row{ display:grid; grid-template-columns:1fr 1fr; gap:8px }
@media (max-width:560px){ .row{ grid-template-columns:1fr } }
.muted{ color:#9fb2c6; font-size:12px }

table{
  width:100%; border-collapse:separate; border-spacing:0;
  border:1px solid var(--line); background:#0c1628; border-radius:10px;
  overflow:hidden;
}
thead th{
  position:sticky; top:0; z-index:1; text-align:left;
  padding:10px; font-size:12px; color:var(--muted);
  background:#0f1a2b; backdrop-filter:blur(4px);
}
tbody td{
  padding:9px 10px; border-top:1px solid var(--line); font-size:13px; vertical-align:middle;
  word-break:break-word;
}
tbody tr:nth-child(even){ background:rgba(255,255,255,.02) }
tbody tr:hover{ background:#0f1a2b }

#machines{ max-height:none; overflow:visible; padding:0; margin-top:6px; }
#history{ max-height: clamp(220px, 42vh, 520px); overflow:auto; }
#users, #csv{ max-height: clamp(120px, 32vh, 320px); overflow:auto; margin-top:6px; }

*::-webkit-scrollbar{ height:10px; width:10px }
*::-webkit-scrollbar-track{ background:transparent }
*::-webkit-scrollbar-thumb{
  background:#2a3b59; border-radius:8px; border:2px solid transparent; background-clip:padding-box;
}
*::-webkit-scrollbar-thumb:hover{ background:#35507b }

.pill{
  display:inline-block; padding:3px 8px; border-radius:999px; font-size:11px;
  background:#1b2742; color:#d6e9ff; border:1px solid #2b3c5a; white-space:nowrap;
}
.pill-ok{ background:#0f2f1d; border-color:#1e5b38; color:#b6f3c8 }
.pill-idle{ background:#161e33; border-color:#2a3b5a; color:#cfe7ff }
.pill-err{ background:#2a1414; border-color:#6e2525; color:#ffc1c1 }

#machines table{ border-radius:10px; background:#0c1628 }
#machines thead th{ padding:10px; font-size:12px }
#machines tbody td{ padding:8px 10px; font-size:13px }
#machines tbody tr{ height:36px }

.grid > .card:nth-of-type(6) .row{
  display:flex !important;
  gap:8px; flex-wrap:wrap; align-items:center; justify-content:flex-start;
  margin:6px 0 4px 0;
}
.grid > .card:nth-of-type(6) .row > div{ display:contents; }
.grid > .card:nth-of-type(6) > div:nth-of-type(2){
  display:flex !important;
  gap:8px; flex-wrap:wrap; align-items:center; justify-content:flex-start;
  margin-top:8px !important;
}
.grid > .card:nth-of-type(6) > div:nth-of-type(2) button{ margin-left:0 !important; }
.grid > .card:nth-of-type(6) button{ white-space:nowrap; }

@media (max-width:720px){
  .card{ padding:12px }
  thead th, tbody td{ padding:10px }
}
a{ color:#9cc4ff; text-decoration:none }
a:hover{ text-decoration:underline }

  </style>
</head>
<body>
  <h2>WMPS Control Panel</h2>
  <div class="grid">

    <div class="card">
      <h3>Machines</h3>
      <div id="machines"></div>
      <div class="muted" style="margin-top:8px;">Prices and default durations come from configuration.</div>
      <button style="margin-top:8px;" onclick="refresh()">Refresh</button>
    </div>

    <div class="card">
      <h3>Transaction History</h3>
      <table id="history"><thead>
        <tr><th>Time (UTC)</th><th>Tenant</th><th>Machine</th><th>Charged</th><th>Balance After</th><th>Minutes</th><th>Success</th></tr>
      </thead><tbody></tbody></table>
      <div class="muted">Showing last 50 entries</div>
    </div>

    <div class="card">
      <h3>Add / Update User</h3>
      <div class="row">
        <div><label>tenant_code</label><input id="u_code" value="123456"/></div>
        <div><label>name</label><input id="u_name" placeholder="Full name"/></div>
      </div>
      <div class="row">
        <div><label>balance</label><input id="u_balance" value="100"/></div>
        <div style="display:flex; align-items:end;"><button onclick="upsert()">Save</button></div>
      </div>
      <div style="margin-top:8px;">
        <button onclick="listAccounts()">List Accounts</button>
      </div>
      <pre id="users">{{}}</pre>
    </div>

    <div class="card">
      <h3>Settings</h3>
      <div class="row">
        <div><label>Washing machines (comma)</label><input id="cfg_wm" placeholder="1,2,3"/></div>
        <div><label>Dryer machines (comma)</label><input id="cfg_dm" placeholder="4,5,6"/></div>
      </div>
      <div class="row">
        <div><label>Washing default minutes</label><input id="cfg_wmin" placeholder="30"/></div>
        <div><label>Dryer default minutes</label><input id="cfg_dmin" placeholder="60"/></div>
      </div>
      <div class="row">
        <div><label>Washing price</label><input id="cfg_wp" placeholder="5"/></div>
        <div><label>Dryer price</label><input id="cfg_dp" placeholder="5"/></div>
      </div>
      <div class="row">
        <div><label>Disabled machines (comma)</label><input id="cfg_disabled" placeholder="e.g. 2,5"/></div>
        <div></div>
      </div>
      <div class="muted">Per-machine override via price_map in options.json</div>
      <div style="margin-top:8px;">
        <button onclick="loadConfig()">Load Config</button>
        <button onclick="saveConfig()" style="margin-left:8px;">Save Config</button>
      </div>
      <pre id="cfg_out">{{}}</pre>
    </div>

    <div class="card">
      <h3>Quick Charge</h3>
      <div class="row">
        <div><label>tenant_code</label><input id="c_tenant" value="123456"/></div>
        <div><label>machine</label><input id="c_machine" value="1"/></div>
      </div>
      <div class="row">
        <div><label>price (optional)</label><input id="c_price" placeholder="leave blank for category/price_map"/></div>
        <div><label>minutes (optional)</label><input id="c_minutes" placeholder="leave blank for default minutes"/></div>
      </div>
      <div style="margin-top:8px;">
        <button onclick="simulate()">Simulate/Charge</button>
        <span class="muted">Honors simulate flag; DI used for availability</span>
      </div>
      <pre id="charge_out">{{}}</pre>
    </div>

    <div class="card">
  <h3>CSV Files</h3>
  <div class="row">
    <div><button onclick="window.location='./download?file=accounts'">Download accounts.csv</button></div>
    <div><button onclick="window.location='./download?file=transactions'">Download transactions.csv</button></div>
    <div>
      <input type="file" id="uploadCsv" accept=".csv" style="display:none" onchange="uploadAuto()"/>
      <button onclick="document.getElementById('uploadCsv').click()">Upload CSV</button>
    </div>
  </div>
  <div style="margin-top:8px;">
    <button onclick="cat('accounts','data')">Open accounts (data)</button>
    <button onclick="cat('transactions','data')" style="margin-left:8px;">Open transactions (data)</button>
  </div>
  <pre id="csv">{{}}</pre>
</div>


  </div>

<script>
function pill2(state, err) {
  let cls='pill-idle', txt='Available';
  if (state==='on' || state==='true' || state==='running') { cls='pill-ok'; txt='Busy'; }
  if (state==='simulated') { cls='pill-idle'; txt='Simulated'; }
  if (state==='disabled' || err==='disabled') { cls='pill-err'; txt='Disabled'; }
  if (err && err!=='disabled') { cls='pill-err'; txt='Error'; }
  return `<span class="pill ${cls}">${txt}</span>`;
}

async function renderMachines() {
  const res = await fetch('./machines');
  const js = await res.json();
  const list = js.machines || [];
  let html = '<table><thead><tr><th>ID</th><th>Category</th><th>Switch</th><th>Sensor</th><th>Status</th><th>Price</th><th>Default minutes</th></tr></thead><tbody>';
  for (const m of list) {
    html += `<tr>
      <td>${m.id}</td>
      <td>${m.category}</td>
      <td>${m.ha_switch}</td>
      <td>${m.ha_sensor}</td>
      <td>${pill2(m.state, m.error)}</td>
      <td>${m.price}</td>
      <td>${m.default_minutes}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  document.getElementById('machines').innerHTML = html;
}

async function renderHistory() {
  const res = await fetch('./history?limit=50');
  const js = await res.json();
  const tbody = document.querySelector('#history tbody');
  tbody.innerHTML = '';
  for (const r of (js.items || [])) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${r.timestamp}</td>
      <td>${r.tenant_code}</td>
      <td>${r.machine_number}</td>
      <td>${r.amount_charged}</td>
      <td>${r.balance_after}</td>
      <td>${r.cycle_minutes}</td>
      <td>${r.success}</td>`;
    tbody.appendChild(tr);
  }
}

async function upsert() {
  const body = {
    tenant_code: document.getElementById('u_code').value.trim(),
    name: document.getElementById('u_name').value.trim(),
    balance: parseFloat(document.getElementById('u_balance').value.trim()||'0')
  };
  const res = await fetch('./accounts/upsert', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const js = await res.json();
  document.getElementById('users').textContent = JSON.stringify(js, null, 2);
}

async function listAccounts() {
  const res = await fetch('./accounts/list');
  const js = await res.json();
  document.getElementById('users').textContent = JSON.stringify(js, null, 2);
}

async function loadConfig() {
  const res = await fetch('./config');
  const js = await res.json();
  document.getElementById('cfg_out').textContent = JSON.stringify(js, null, 2);
  document.getElementById('cfg_wm').value = (js.washing_machines||[]).join(',');
  document.getElementById('cfg_dm').value = (js.dryer_machines||[]).join(',');
  document.getElementById('cfg_wmin').value = js.washing_minutes||30;
  document.getElementById('cfg_dmin').value = js.dryer_minutes||60;
  document.getElementById('cfg_wp').value = js.price_washing||5;
  document.getElementById('cfg_dp').value = js.price_dryer||5;
  document.getElementById('cfg_disabled').value = (js.disabled_machines||[]).join(',');
}

async function saveConfig() {
  const wm = document.getElementById('cfg_wm').value.split(',').map(s=>parseInt(s.trim())).filter(n=>!isNaN(n));
  const dm = document.getElementById('cfg_dm').value.split(',').map(s=>parseInt(s.trim())).filter(n=>!isNaN(n));
  const body = {
    washing_machines: wm, dryer_machines: dm,
    washing_minutes: parseInt(document.getElementById('cfg_wmin').value||'30'),
    dryer_minutes: parseInt(document.getElementById('cfg_dmin').value||'60'),
    price_washing: parseFloat(document.getElementById('cfg_wp').value||'5'),
    price_dryer: parseFloat(document.getElementById('cfg_dp').value||'5'),
    disabled_machines: document.getElementById('cfg_disabled').value.split(',').map(s=>parseInt(s.trim())).filter(n=>!isNaN(n))
  };
  const res = await fetch('./config', { method:'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  const js = await res.json();
  document.getElementById('cfg_out').textContent = JSON.stringify(js, null, 2);
  await renderMachines();
}

async function simulate() {
  const t = document.getElementById('c_tenant').value.trim();
  const m = document.getElementById('c_machine').value.trim();
  const p = document.getElementById('c_price').value.trim();
  const mins = document.getElementById('c_minutes').value.trim();
  const params = new URLSearchParams({ tenant_code: t, machine: m });
  if (p) params.set('price', p);
  if (mins) params.set('minutes', mins);
  const res = await fetch('./simulate/charge?' + params.toString());
  const txt = await res.text();
  document.getElementById('charge_out').textContent = txt;
  await renderHistory();
}

async function cat(file, where) {
  const res = await fetch(`./debug/cat?file=${file}&where=${where}`);
  const txt = await res.text();
  document.getElementById('csv').textContent = txt;
}

async function uploadAuto() {
  const input = document.getElementById('uploadCsv');
  const file = input.files[0];
  if (!file) { alert('No file selected'); return; }

  // read file as raw bytes (no multipart needed)
  const buf = await file.arrayBuffer();

  const res = await fetch('./upload_auto', {
    method: 'PUT',
    body: buf
  });

  if (!res.ok) {
    const txt = await res.text();
    alert('Upload failed: ' + txt);
    return;
  }

  const js = await res.json();
  // js.target is "accounts" or "transactions"
  alert(`Uploaded as ${js.target}.csv (${js.bytes} bytes)`);

  // refresh preview and history
  await cat(js.target, 'data');
  await renderHistory();

  // clear input so the same filename can be re-uploaded later
  input.value = '';
}



async function refresh() { await renderMachines(); await renderHistory(); }
refresh(); loadConfig();
</script>
</body>
</html>
""")

@app.get("/machines")
def machines():
    opts = _read_options()
    wm = [str(x) for x in (opts.get("washing_machines") or [1, 2, 3])]
    dm = [str(x) for x in (opts.get("dryer_machines") or [4, 5, 6])]
    ids = wm + dm

    has_token = bool(_resolve_token(opts.get("ha_token")))  # env-aware token check
    mode = str(opts.get("do_mode") or "pulse").lower()
    invert_di = bool(opts.get("invert_di"))
    out = []

    def _soft_busy(mid: str) -> bool:
        try:
            return time.time() < float(ACTIVE_UNTIL.get(mid, 0))
        except Exception:
            return False

    for mid in sorted(ids, key=lambda s: int(s)):
        switch, sensor = _machine_entities(mid, opts)
        try_price = _price_for(mid, opts)

        # Defaults
        state = "unknown"
        busy_source = "unknown"

        # Disabled machine
        if not machine_enabled(mid, opts):
            err = "disabled"
            state = "disabled"
        else:
            # Prepare sources
            has_adam = bool((opts.get("adam_host") or "").strip())
            r, di = _machine_adam_mapping(mid, opts)
            di_val = None  # True=active, False=inactive, None=unknown

            # Read ADAM DI (if present) and optionally invert
            if has_adam and di is not None:
                raw = _adam_read_di(di, opts)  # True/False/None expected
                if raw is not None:
                    di_val = bool(raw)
                    if invert_di:
                        di_val = not di_val

            # Read from HA sensor (if token present)
            ha_state = None
            if has_token:
                ha_state = _get_state(sensor, opts)  # strings like "on"/"off"/"unknown"

            # Timer-based soft busy
            soft = _soft_busy(mid)

            # ---- Decision logic ----
            if mode == "hold":
                # 1) DI has priority
                if di_val is True:
                    state, busy_source = "on", "di"
                elif di_val is False:
                    # DI inactive; if timer exists use it, otherwise fall back to HA
                    if soft:
                        state, busy_source = "on", "timer"
                    elif ha_state in ("on", "off"):
                        state, busy_source = ha_state, "ha"
                    else:
                        state, busy_source = "off", "di"
                else:
                    # DI unknown -> prefer HA, then timer
                    if ha_state in ("on", "off"):
                        state, busy_source = ha_state, "ha"
                    elif soft:
                        state, busy_source = "on", "timer"
                    else:
                        state, busy_source = "unknown", "none"
            else:
                # mode == "pulse"
                # 1) Timer has priority (busy during purchased time)
                if soft:
                    state, busy_source = "on", "timer"
                else:
                    # If no timer: prefer DI; otherwise HA; otherwise assume "off"
                    if di_val is True:
                        state, busy_source = "on", "di"
                    elif di_val is False:
                        state, busy_source = "off", "di"
                    elif ha_state in ("on", "off"):
                        state, busy_source = ha_state, "ha"
                    else:
                        state, busy_source = "off", "none"

            err = (
                "disabled"
                if not machine_enabled(mid, opts)
                else ("" if (opts.get("simulate") or has_token or (opts.get("adam_host") or "").strip()) else "no_token")
            )

        out.append({
            "id": mid,
            "category": "washing" if mid in wm else "dryer",
            "ha_switch": switch,
            "ha_sensor": sensor,
            "state": state,                 # "on"/"off"/"disabled"/"unknown"
            "busy_source": busy_source,     # "di"/"timer"/"ha"/"none"/"unknown" (diagnostics)
            "price": try_price,
            "default_minutes": _default_minutes_for(mid, opts),
            "error": err
        })

    return {"ok": True, "machines": out, "simulate": bool(opts.get("simulate", False))}



# ----------------------- Accounts ------------------------
@app.post("/accounts/upsert")
def accounts_upsert(acc: dict):
    ensure_bootstrap_files()
    required = {"tenant_code","balance"}
    if not required.issubset(acc.keys()):
        raise HTTPException(status_code=400, detail="tenant_code and balance required")
    tenant_code = str(acc.get("tenant_code")).strip()
    name = str(acc.get("name") or "").strip()
    try:
        balance = float(acc.get("balance"))
    except Exception:
        raise HTTPException(status_code=400, detail="balance must be a number")
    with file_lock(GLOBAL_LOCK, timeout=10.0):
        accounts = read_accounts()
        prev = accounts.get(tenant_code, {})
        accounts[tenant_code] = {
            "name": name or prev.get("name",""),
            "balance": float(balance),
            "last_transaction_utc": prev.get("last_transaction_utc","")
        }
        write_accounts(accounts)
    return {"ok": True, "tenant_code": tenant_code, "name": name, "balance": float(balance)}

@app.get("/accounts/list")
def accounts_list():
    return {"ok": True, "accounts": read_accounts()}

# ----------------------- Config --------------------------
@app.get("/config")
def get_config():
    opts = _read_options()
    keys = [
        "washing_machines","dryer_machines","washing_minutes","dryer_minutes",
        "price_washing","price_dryer","price_map","disabled_machines","simulate",
        "ha_url","tts_service","media_player",
        "keypad_source","ha_ws_url","ha_event_type",
        "adam_host","adam_port","adam_unit_id","do_mode","pulse_seconds","invert_di",
        "activation_confirm_timeout_s"
    ]
    return {k: opts.get(k) for k in keys}

@app.post("/config")
def set_config(payload: dict):
    opts = _read_options()
    patch = dict(payload)
    if "washing_machines" in patch:
        patch["washing_machines"] = [int(x) for x in patch["washing_machines"]]
    if "dryer_machines" in patch:
        patch["dryer_machines"] = [int(x) for x in patch["dryer_machines"]]
    if "disabled_machines" in patch:
        patch["disabled_machines"] = [int(x) for x in patch["disabled_machines"]]
    opts.update(patch)
    _write_options(opts)
    return {"ok": True, "saved": patch}

# ----------------------- Charge flow ---------------------
def _machine_lock_path(mid: str) -> Path:
    return LOCK_DIR / f"machine_{mid}.lock"

@app.post("/charge")
def charge(req: dict):
    ensure_bootstrap_files()
    try:
        tenant_code = str(req.get("tenant_code")).strip()
        machine = str(req.get("machine")).strip()
    except Exception:
        raise HTTPException(status_code=400, detail="INVALID_PAYLOAD")

    opts = _read_options()
    price = req.get("price", None)
    minutes = req.get("cycle_minutes", None)

    return _handle_charge(
        tenant_code=tenant_code,
        machine=machine,
        price=price,
        minutes=minutes,
        opts=opts
    )

@app.get("/simulate/charge")
def simulate_charge(
    tenant_code: str = Query(...),
    machine: str = Query(...),
    price: Optional[float] = Query(None, ge=0.0),
    minutes: Optional[int] = Query(None, ge=1)
):
    req = {"tenant_code": tenant_code, "machine": machine, "price": price, "cycle_minutes": minutes}
    try:
        res = charge(req)
        return res
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": e.detail}, status_code=e.status_code)

# ----------------------- History & Debug -----------------
@app.get("/history")
def history(limit: int = Query(50, ge=1, le=1000)):
    return {"ok": True, "items": tail_transactions(limit)}

@app.get("/debug/cat")
def debug_cat(file: str = Query(..., pattern="^(accounts|transactions)$"), where: str = Query("data")):
    path = (ACCOUNTS_PATH if file=="accounts" else TRANSACTIONS_PATH)
    if where == "share":
        path = (SHARE_ACCOUNTS if file=="accounts" else SHARE_TX)
    if not path.exists():
        return JSONResponse({"error": f"{path} not found"}, status_code=404)
    return PlainTextResponse(path.read_text(encoding="utf-8"))

@app.get("/debug/where")
def debug_where():
    def info(p: Path):
        try:
            return {"path": str(p), "exists": p.exists(), "size": p.stat().st_size if p.exists() else 0, "mtime": p.stat().st_mtime if p.exists() else 0}
        except Exception as e:
            return {"path": str(p), "error": str(e)}
    return {
        "data": {"accounts": info(ACCOUNTS_PATH), "transactions": info(TRANSACTIONS_PATH)},
        "share": {"accounts": info(SHARE_ACCOUNTS), "transactions": info(SHARE_TX)}
    }

@app.get("/download")
def download(file: str = Query(..., pattern="^(accounts|transactions)$")):
    path = ACCOUNTS_PATH if file=="accounts" else TRANSACTIONS_PATH
    if not path.exists():
        raise HTTPException(status_code=404, detail="FILE_NOT_FOUND")
    filename = f"{file}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    def iterfile():
        with path.open("rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk: break
                yield chunk
    return StreamingResponse(iterfile(), media_type="text/csv", headers=headers)

# ----------------------- Upload --------------------------
@app.put("/upload_raw")
async def upload_raw(
    request: Request,
    target: str | None = Query(
        None,
        pattern="^(accounts|transactions)$",
        description="Optional. If omitted, the server will auto-detect by CSV header."
    ),
):
    """
    Accept a raw CSV (bytes). If 'target' is omitted, auto-detect which CSV it is
    by inspecting the header. Write atomically under /data and mirror to /share/wmps.
    """
    ensure_bootstrap_files()

    # --- read body (raw bytes) ---
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="EMPTY_BODY")
    if len(body) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="FILE_TOO_LARGE")

    # Normalize text: strip UTF-8 BOM and unify newlines
    text = body.decode("utf-8-sig", errors="ignore").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines()
    if not lines:
        raise HTTPException(status_code=400, detail="EMPTY_FILE")

    header = lines[0].strip().lower()

    # --- auto-detect if needed ---
    detected: str | None = None
    def has_all(cols: list[str]) -> bool:
        return all(col in header for col in cols)

    if target is None:
        if has_all(["tenant_code", "balance", "last_transaction_utc"]):
            detected = "accounts"
        elif has_all(["timestamp", "tenant_code", "machine_number"]):
            detected = "transactions"
        else:
            raise HTTPException(status_code=400, detail="UNKNOWN_CSV_FORMAT")
        target = detected

    # --- choose paths ---
    path = ACCOUNTS_PATH if target == "accounts" else TRANSACTIONS_PATH
    tmp = path.with_suffix(".csv.up")

    # --- light header validation (prevent wrong file overwrite) ---
    if target == "accounts":
        if not has_all(["tenant_code", "balance"]):
            raise HTTPException(status_code=400, detail="INVALID_ACCOUNTS_HEADER")
    else:  # transactions
        if not has_all(["timestamp", "tenant_code", "machine_number"]):
            raise HTTPException(status_code=400, detail="INVALID_TRANSACTIONS_HEADER")

    # --- atomic write with backup + mirror ---
    data_bytes = text.encode("utf-8")  # write normalized content
    with file_lock(GLOBAL_LOCK, timeout=10.0):
        # optional backup of current file (best-effort)
        try:
            if path.exists():
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                bak = path.with_suffix(f".csv.bak.{ts}")
                bak.write_bytes(path.read_bytes())
        except Exception:
            pass

        with tmp.open("wb") as f:
            f.write(data_bytes)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)

        # keep mirrors in sync
        if target == "accounts":
            _mirror(ACCOUNTS_PATH, SHARE_ACCOUNTS)
        else:
            _mirror(TRANSACTIONS_PATH, SHARE_TX)

    return {"ok": True, "target": target, "bytes": len(data_bytes)}

@app.put("/upload_auto")
async def upload_auto(
    request: Request,
    target: str | None = Query(
        None,
        pattern="^(accounts|transactions)$",
        description="Optional. If omitted, the server will auto-detect by CSV header."
    ),
):
    # Delegate to upload_raw so both endpoints behave the same
    return await upload_raw(request, target)
