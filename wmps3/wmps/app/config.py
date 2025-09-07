"""
Configuration loader for the Washing Machine Payment System
- Primary source: /data/options.json (HA Add-on Options UI)
- Fallback: configs/config.yaml (for local dev)
- Supports top-level toggles: machine_1_enabled .. machine_6_enabled
  and merges them into machines[*].enabled so the app enforces them.
- Adds support for ADAM DO/DI, HA-keypad events, and per-machine mapping.
"""

from __future__ import annotations

import os
import json
import yaml
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, ValidationError, Field

from app.utils import logger


# ----------------------------
# Pydantic models (Pydantic v2)
# ----------------------------
class AdamConfig(BaseModel):
    enabled: bool = False
    host: str = "192.168.1.101"
    port: int = 502
    unit_id: int = 1
    do_mode: Optional[str] = None       # "pulse" | "hold"
    pulse_seconds: Optional[float] = None
    invert_di: Optional[bool] = None
    activation_confirm_timeout_s: Optional[int] = None


class HAConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://supervisor/core"
    ws_url: Optional[str] = None
    token: str = ""
    tts_service: str = "tts.speak"
    media_player: str = "media_player.vlc_telnet"
    keypad_source: Optional[str] = None          # "ha" | "evdev" | "auto"
    ha_event_type: Optional[str] = None          # keyboard_remote_command_received
    confirm_keys: Optional[List[int]] = None     # [28, 96] default at normalize


class MachineListItem(BaseModel):
    id: int
    enabled: bool = True

    # Labels & pricing
    label: Optional[str] = None
    cycle_minutes: Optional[int] = None
    price: Optional[float] = None

    # HA entity route (optional)
    ha_switch: Optional[str] = None
    ha_sensor: Optional[str] = None

    # ADAM mapping (optional)
    relay: Optional[int] = None
    di: Optional[int] = None
    do_mode: Optional[str] = None
    invert_di: Optional[bool] = None


class MachineConfig(BaseModel):
    cycle_minutes: int = 30
    price: float = 5.0


class SecurityConfig(BaseModel):
    code_entry_timeout_s: int = 30
    max_failed_attempts: int = 5
    lockout_seconds: int = 120


class AppConfig(BaseModel):
    # ---- Flat/common (add-on options.json) ----
    ha_url: Optional[str] = None
    ha_ws_url: Optional[str] = None
    ha_token: Optional[str] = None
    tts_service: Optional[str] = None
    media_player: Optional[str] = None

    keypad_source: Optional[str] = None
    ha_event_type: Optional[str] = None
    confirm_keys: Optional[List[int]] = None

    adam_host: Optional[str] = None
    adam_port: Optional[int] = None
    adam_unit_id: Optional[int] = 1
    do_mode: Optional[str] = None
    pulse_seconds: Optional[float] = None
    invert_di: Optional[bool] = None
    activation_confirm_timeout_s: Optional[int] = None

    simulate: bool = False

    # Category defaults
    washing_minutes: Optional[int] = None
    dryer_minutes: Optional[int] = None
    price_washing: Optional[float] = None
    price_dryer: Optional[float] = None

    # Legacy single price (kept for backward compat; prefer price_* above)
    price_per_cycle: Optional[int] = None

    # Machines
    machines: List[MachineListItem] | None = None            # options.json list style
    machines_yaml: Dict[str, MachineConfig] = Field(default_factory=dict)  # yaml dict style

    # Legacy nested blocks
    adam: AdamConfig | None = None
    ha: HAConfig | None = None
    security: SecurityConfig = SecurityConfig()

    # Persistence
    accounts_file: str = "/data/accounts.csv"
    transactions_file: str = "/data/transactions.csv"


# ----------------------------
# Normalization helpers
# ----------------------------
def _derive_ws_url_from_http(http_url: Optional[str]) -> Optional[str]:
    if not http_url:
        return None
    url = http_url.strip().rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + "/api/websocket"
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):] + "/api/websocket"
    return url + "/api/websocket"


def _resolve_supervisor_token(candidate: Optional[str]) -> str:
    # Prefer explicit config if provided and non-empty
    if candidate and candidate.strip():
        return candidate.strip()
    # Fallback to Supervisor-provided environment tokens
    env_token = os.getenv("SUPERVISOR_TOKEN") or os.getenv("HASSIO_TOKEN") or ""
    return env_token.strip()


def _coerce_confirm_keys(val: Any) -> List[int]:
    # Accept proper list
    if isinstance(val, list) and all(isinstance(x, int) for x in val):
        return val
    # Some schemas can send a sentinel like "int" – coerce to defaults
    if isinstance(val, str):
        return [28, 96]
    # Default
    return [28, 96]


def _apply_machine_toggles(norm: Dict[str, Any]) -> None:
    """Merge top-level machine_N_enabled switches into machines list and
    ensure relay/di defaults if entries are auto-created."""
    toggles: Dict[int, bool] = {}
    for i in range(1, 7):
        key = f"machine_{i}_enabled"
        if key in norm:
            try:
                toggles[i] = bool(norm[key])
            except Exception:
                pass
    if not toggles:
        return

    machines = norm.get("machines")
    if not isinstance(machines, list):
        machines = []
        norm["machines"] = machines

    by_id: Dict[int, Dict[str, Any]] = {}
    for m in machines:
        try:
            mid = int(m.get("id")) if isinstance(m, dict) else int(getattr(m, "id", 0))
        except Exception:
            continue
        if mid:
            by_id[mid] = m  # keep ref

    for mid, enabled in toggles.items():
        if mid in by_id:
            entry = by_id[mid]
            if isinstance(entry, dict):
                entry["enabled"] = enabled
                if entry.get("relay") is None:
                    entry["relay"] = mid - 1
                if entry.get("di") is None:
                    entry["di"] = mid - 1
            else:
                by_id[mid] = {"id": mid, "relay": mid - 1, "di": mid - 1, "enabled": enabled}
        else:
            machines.append({"id": mid, "relay": mid - 1, "di": mid - 1, "enabled": enabled})


def _normalize_sources(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize raw dict to AppConfig-compatible keys regardless of source style."""
    norm = dict(raw)

    # Machines YAML dict -> machines_yaml
    if isinstance(norm.get("machines"), dict):
        norm["machines_yaml"] = norm.get("machines")
        norm["machines"] = None

    # Promote legacy nested blocks
    if isinstance(norm.get("adam"), dict):
        a = norm["adam"]
        norm.setdefault("adam_host", a.get("host"))
        norm.setdefault("adam_port", a.get("port"))
        norm.setdefault("adam_unit_id", a.get("unit_id", 1))
        for k in ("do_mode", "pulse_seconds", "invert_di", "activation_confirm_timeout_s"):
            if a.get(k) is not None:
                norm.setdefault(k, a.get(k))

    if isinstance(norm.get("ha"), dict):
        h = norm["ha"]
        norm.setdefault("ha_url", h.get("base_url"))
        norm.setdefault("ha_ws_url", h.get("ws_url"))
        norm.setdefault("ha_token", h.get("token"))
        norm.setdefault("tts_service", h.get("tts_service"))
        norm.setdefault("media_player", h.get("media_player"))
        if h.get("keypad_source") is not None:
            norm.setdefault("keypad_source", h.get("keypad_source"))
        if h.get("ha_event_type") is not None:
            norm.setdefault("ha_event_type", h.get("ha_event_type"))
        if h.get("confirm_keys") is not None:
            norm.setdefault("confirm_keys", h.get("confirm_keys"))

    # Default to Supervisor endpoints if missing
    norm.setdefault("ha_url", "http://supervisor/core/api")
    if not norm.get("ha_ws_url") and norm.get("ha_url"):
        norm["ha_ws_url"] = _derive_ws_url_from_http(norm.get("ha_url"))

    # Token resolution: empty/None -> Supervisor/Hassio token from env
    norm["ha_token"] = _resolve_supervisor_token(norm.get("ha_token"))

    # Keypad defaults and coercion
    norm.setdefault("keypad_source", "ha")
    norm.setdefault("ha_event_type", "keyboard_remote_command_received")
    norm["confirm_keys"] = _coerce_confirm_keys(norm.get("confirm_keys"))

    # Merge top-level machine toggles
    _apply_machine_toggles(norm)

    return norm


# ----------------------------
# Logging
# ----------------------------
def _log_summary(cfg: AppConfig, source: str) -> None:
    try:
        if cfg.machines is not None:
            m = ",".join(f"{x.id}:{'1' if x.enabled else '0'}" for x in cfg.machines)
        else:
            m = ",".join(sorted(cfg.machines_yaml.keys())) if cfg.machines_yaml else "none"
    except Exception:
        m = "unknown"

    logger.info("[WMPS] Configuration loaded from %s", source)
    logger.info(
        "[WMPS] Summary: simulate=%s, keypad_source=%s, do_mode=%s, invert_di=%s, machines=%s",
        cfg.simulate, cfg.keypad_source, cfg.do_mode, cfg.invert_di, m or "none",
    )
    logger.info(
        "[WMPS] Files: accounts=%s, transactions=%s",
        cfg.accounts_file, cfg.transactions_file
    )


# ----------------------------
# Public API
# ----------------------------
def load_config(path: str | None = None) -> AppConfig:
    options_path = "/data/options.json"
    if os.path.exists(options_path):
        try:
            with open(options_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
            norm = _normalize_sources(raw)
            cfg = AppConfig(**norm)
            _log_summary(cfg, options_path)
            return cfg
        except Exception as e:
            msg = f"Failed to parse {options_path}: {e}"
            logger.error(msg)
            raise ValueError(msg) from e

    cfg_path = path or os.path.join("configs", "config.yaml")
    if not os.path.exists(cfg_path):
        msg = f"Configuration file not found: {cfg_path}"
        logger.error(msg)
        raise FileNotFoundError(msg)

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        norm = _normalize_sources(raw)
        cfg = AppConfig(**norm)
        _log_summary(cfg, cfg_path)
        return cfg
    except ValidationError as ve:
        msg = f"Configuration validation failed: {ve}"
        logger.error(msg)
        raise ValueError(msg) from ve
    except Exception as e:
        msg = f"Failed to read YAML at {cfg_path}: {e}"
        logger.error(msg)
        raise ValueError(msg) from e
