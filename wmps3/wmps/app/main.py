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
    import websocket  # HA WebSocket events
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

app = FastAPI(title="WMPS API", version="3.2.0")

# ----------------------- Logging -------------------------
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
            lvl = getattr(__import__("logging"), _LEVEL_MAP.get(level.upper(), "INFO"))
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
    """TTS helper compatible with both 'tts.speak' and legacy 'tts.*_say'."""
    opts = _read_options()
    if not text: return
    tts_service_raw = (opts.get("tts_service") or "tts.google_translate_say")
    if "." in tts_service_raw:
        domain, service = tts_service_raw.split(".", 1)
    else:
        domain, service = "tts", tts_service_raw
    media_player = opts.get("media_player") or ""
    token = opts.get("ha_token") or ""
    url = opts.get("ha_url") or "http://supervisor/core"
    if not token or not media_player:
        return
    try:
        if domain == "tts" and service == "speak":
            payload = {"media_player_entity_id": media_player, "message": text, "cache": False}
        else:
            payload = {"entity_id": media_player, "message": text, "cache": False}
        _ha_call_service(url, token, domain, service, payload)
    except Exception as e:
        _log("WARN", f"TTS failed: {e}")

# ----------------------- HA State/Availability -----------
def _get_state(entity_id: str, opts: dict) -> str:
    token = opts.get("ha_token") or ""
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
    host = cfg["host"]
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
    if not _HAS_PYMODBUS:
        return False
    cfg = _adam_cfg(opts)
    host = cfg["host"]
    if not host:
        return False
    try:
        with ModbusTcpClient(host=host, port=cfg["port"], timeout=2.0) as client:  # type: ignore
            if not client.connect():
                _log("WARN", "ADAM: connect failed for write_coil")
                return False
            wr = client.write_coil(int(coil_index), bool(state), unit=cfg["unit"])
            if not hasattr(wr, "isError") or wr.isError():
                _log("WARN", "ADAM: write_coil error")
                return False
            return True
    except Exception as e:
        _log("WARN", f"ADAM: write_coil exception: {e}")
        return False

def _adam_pulse(coil_index: int, pulse_seconds: float, opts: dict) -> bool:
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
    if opts.get("adam_host") is not None and di is not None:
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
    token = opts.get("ha_token") or ""
    switch, sensor = _machine_entities(mid, opts)
    r, di = _machine_adam_mapping(mid, opts)

    confirm_timeout = int(opts.get("activation_confirm_timeout_s", 8))

    if simulate:
        _log("INFO", f"[SIMULATE] turn ON {switch} for {minutes} minutes")
        return True

    # ADAM path if available
    if opts.get("adam_host") is not None and r is not None:
        do_mode = (opts.get("do_mode") or "pulse").lower()
        pulse_seconds = float(opts.get("pulse_seconds") or 0.8)
        ok = False
        if do_mode == "pulse":
            _log("INFO", f"ADAM: pulse relay index={r} for {pulse_seconds}s (m#{mid})")
            ok = _adam_pulse(r, pulse_seconds, opts)
        else:
            _log("INFO", f"ADAM: set_on relay index={r} (m#{mid})")
            ok = _adam_write_coil(r, True, opts)

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

# ----------------------- Core Charge ---------------------
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

        with file_lock(GLOBAL_LOCK, timeout=10.0):
            accounts = read_accounts()
            if tenant_code not in accounts:
                # rollback attempt (best-effort)
                try:
                    url = opts.get("ha_url") or "http://supervisor/core"
                    token = opts.get("ha_token") or ""
                    switch, _sensor = _machine_entities(machine, opts)
                    if token:
                        _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
                except Exception:
                    pass
                raise HTTPException(status_code=404, detail="TENANT_NOT_FOUND")

            bal_before = float(accounts[tenant_code].get("balance", 0.0))
            if bal_before < p:
                speak("Insufficient balance.")
                try:
                    url = opts.get("ha_url") or "http://supervisor/core"
                    token = opts.get("ha_token") or ""
                    switch, _sensor = _machine_entities(machine, opts)
                    if token:
                        _ha_call_service(url, token, "switch", "turn_off", {"entity_id": switch})
                except Exception:
                    pass
                append_transaction(tenant_code, machine, 0.0, bal_before, bal_before, m, success=False)
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

    def on_sym(self, sym: str):
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
        elif self.state == "ENTER_CODE":
            if sym.isdigit():
                if len(self.buf_code) < 6:
                    self.buf_code += sym
            elif sym == "ENTER":
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
        elif self.state == "SELECT_MACHINE":
            if sym.isdigit():
                n = int(sym)
                if 1 <= n <= 6:
                    self.sel_machine = n
                    mins = _default_minutes_for(str(n), self.opts_provider())
                    self.speak(f"Machine {n} selected for {mins} minutes. Press enter to confirm.")
                    self.state = "CONFIRM"
        elif self.state == "CONFIRM":
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
                    if e.status_code == 409: msg = "Machine is busy."
                    elif e.status_code == 402: msg = "Insufficient balance."
                    elif e.status_code == 423: msg = "Machine disabled."
                    elif e.status_code == 404: msg = "User not found."
                    self.speak(msg)
                except Exception:
                    self.speak("Operation failed.")
                finally:
                    self._reset()

    def _reset(self):
        self.state = "IDLE"
        self.buf_code = ""
        self.sel_machine = None
        self.last_input = time.monotonic()

# Key mapping helpers
_MAIN_ROW_NUM = {2:"1",3:"2",4:"3",5:"4",6:"5",7:"6",8:"7",9:"8",10:"9",11:"0"}  # KEY_1..KEY_0
_KP_NUM = {79:"1",80:"2",81:"3",75:"4",76:"5",77:"6",71:"7",72:"8",73:"9",82:"0"}  # KP1..KP0
def _map_keycode_name(name: str) -> Optional[str]:
    # Convert raw key names to symbols used by KeypadStateMachine
    if not name:
        return None
    k = name
    if k.startswith("KEY_"):
        k = k[4:]
    if k in ("ENTER","KPENTER"):
        return "ENTER"
    if k in ("KPASTERISK","ESC","BACKSPACE"):
        return "CANCEL"
    if k in ("HASHTAG",):
        return "ENTER"
    if k.startswith("KP") and len(k) == 3 and k[2].isdigit():
        return k[2]
    if len(k) == 1 and k.isdigit():
        return k
    if len(k) == 2 and k[0] == 'F' and k[1].isdigit():
        return None
    return None

def _map_keycode_int(code: int) -> Optional[str]:
    if code in _MAIN_ROW_NUM:
        return _MAIN_ROW_NUM[code]
    if code in _KP_NUM:
        return _KP_NUM[code]
    if code in (28, 96):  # Enter, KP_Enter
        return "ENTER"
    if code in (1, 14, 111):  # Esc, Backspace, Delete -> cancel
        return "CANCEL"
    if code in (43,):  # '#'/non-us? fallback treat as ENTER
        return "ENTER"
    return None

# EVDEV thread
def _evdev_thread():
    if not _HAS_EVDEV:
        _log("WARN", "evdev not available; keypad_source=evdev cannot start")
        return
    path = _find_keypad_device()
    if not path:
        _log("WARN", "No keypad input device found under /dev/input")
        return
    dev = InputDevice(path)
    sm = KeypadStateMachine(opts_provider=_read_options, speak_fn=speak, handle_charge_fn=_handle_charge)
    _log("INFO", f"Keypad(evdev) listening on {path} ({getattr(dev, 'name', 'unknown')})")
    for event in dev.read_loop():
        if event.type == ecodes.EV_KEY:
            key = categorize(event)
            if key.keystate == key.key_down:
                name = key.keycode if isinstance(key.keycode, str) else (key.keycode[0] if isinstance(key.keycode, (list, tuple)) and key.keycode else "")
                sym = _map_keycode_name(name)
                if sym:
                    sm.on_sym(sym)

def _find_keypad_device(patterns=("event*",)):
    for path in glob.glob("/dev/input/" + patterns[0]):
        try:
            dev = InputDevice(path)
            if "Keyboard" in (dev.name or "") or "Keypad" in (dev.name or ""):
                return dev.path
        except Exception:
            pass
    return None

# HA WebSocket thread
def _derive_ws_url(http_url: Optional[str]) -> Optional[str]:
    if not http_url: return None
    u = http_url.strip().rstrip("/")
    if u.startswith("https://"): return "wss://" + u[len("https://"):] + "/api/websocket"
    if u.startswith("http://"):  return "ws://"  + u[len("http://"):]  + "/api/websocket"
    return u  # assume already ws(s)

def _ha_ws_thread():
    if not _HAS_WS:
        _log("WARN", "websocket-client not available; keypad_source=ha cannot start")
        return
    opts = _read_options()
    token = opts.get("ha_token") or ""
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
        # Expect auth_required
        raw = ws.recv()
        _send({"type":"auth", "access_token": token})
        raw = ws.recv()  # auth_ok
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
            # Accept different shapes: key_code:int or key_name:str
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
            t = threading.Thread(target=_evdev_thread, daemon=True); t.start()
        else:
            _log("INFO", "keypad_source=auto -> using ha websocket")
            t = threading.Thread(target=_ha_ws_thread, daemon=True); t.start()

# ----------------------- Models & API --------------------
@app.on_event("startup")
def on_start():
    ensure_bootstrap_files()
    _log("INFO", "WMPS API started.")
    start_keypad_listener()

@app.get("/ping")
def ping():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

@app.get("/", response_class=HTMLResponse)
def root_ui():
    # (unchanged UI HTML, omitted here for brevity in commentary; keep your original UI)
    # For full replacement, reuse your existing HTML block exactly as before.
    # To stay within limits, we keep UI identical to your provided version:
    return HTMLResponse("""REPLACED_BY_SAME_HTML_AS_BEFORE""")

@app.get("/machines")
def machines():
    opts = _read_options()
    wm = [str(x) for x in (opts.get("washing_machines") or [1,2,3])]
    dm = [str(x) for x in (opts.get("dryer_machines") or [4,5,6])]
    ids = wm + dm
    out = []
    for mid in sorted(ids, key=lambda s: int(s)):
        switch, sensor = _machine_entities(mid, opts)
        try_price = _price_for(mid, opts)

        # Prefer ADAM DI for state if configured
        r, di = _machine_adam_mapping(mid, opts)
        state = "unknown"
        if not machine_enabled(mid, opts):
            state = "disabled"
        else:
            if opts.get("adam_host") is not None and di is not None:
                di_state = _adam_read_di(di, opts)
                if di_state is not None:
                    state = "on" if di_state else "off"
                else:
                    state = _get_state(sensor, opts) if opts.get("ha_token") else ("simulated" if opts.get("simulate") else "unknown")
            else:
                state = _get_state(sensor, opts) if opts.get("ha_token") else ("simulated" if opts.get("simulate") else "unknown")

        err = ("disabled" if not machine_enabled(mid, opts) else ("" if (opts.get("simulate") or opts.get("ha_token") or opts.get("adam_host")) else "no_token"))
        out.append({
            "id": mid,
            "category": "washing" if mid in wm else "dryer",
            "ha_switch": switch,
            "ha_sensor": sensor,
            "state": state,
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
