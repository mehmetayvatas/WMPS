
"""
Configuration loader for the Washing Machine Payment System
- Primary source: /data/options.json (HA Add-on Options UI)
- Fallback: configs/config.yaml (for local dev)
- Supports top-level toggles: machine_1_enabled .. machine_6_enabled
  and merges them into machines[*].enabled so the app enforces them.
"""

from __future__ import annotations

import os
import json
import yaml
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, ValidationError, Field

from app.utils import logger


class AdamConfig(BaseModel):
    enabled: bool = False
    host: str = "192.168.1.101"
    port: int = 502
    unit_id: int = 1


class HAConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://supervisor/core"
    token: str = ""
    tts_service: str = "speak"
    media_player: str = "media_player.vlc_telnet"


class MachineListItem(BaseModel):
    id: int
    relay: int
    enabled: bool = True  # default True if not provided


class MachineConfig(BaseModel):
    cycle_minutes: int = 30
    price: float = 5.0


class SecurityConfig(BaseModel):
    code_entry_timeout_s: int = 30
    max_failed_attempts: int = 5
    lockout_seconds: int = 120


class AppConfig(BaseModel):
    # Flat/common
    ha_url: Optional[str] = None
    ha_token: Optional[str] = None
    adam_host: Optional[str] = None
    adam_port: Optional[int] = None
    adam_unit_id: Optional[int] = 1
    tts_service: Optional[str] = None
    media_player: Optional[str] = None

    simulate: bool = False
    price_per_cycle: int = 30

    # Two styles for machines
    machines: List[MachineListItem] | None = None           # options.json list style
    machines_yaml: Dict[str, MachineConfig] = Field(default_factory=dict)  # yaml dict style

    # Legacy nested (yaml)
    adam: AdamConfig | None = None
    ha: HAConfig | None = None
    security: SecurityConfig = SecurityConfig()

    # Persistence
    accounts_file: str = "/data/accounts.csv"
    transactions_file: str = "/data/transactions.csv"


def _apply_machine_toggles(norm: Dict[str, Any]) -> None:
    """Merge top-level machine_N_enabled switches into machines list."""
    # Collect top-level toggles if present
    toggles: Dict[int, bool] = {}
    for i in range(1, 7):
        key = f"machine_{i}_enabled"
        if key in norm:
            try:
                toggles[i] = bool(norm[key])
            except Exception:
                pass

    # Nothing to do?
    if not toggles:
        return

    # Ensure machines list exists
    machines = norm.get("machines")
    if not isinstance(machines, list):
        machines = []
        norm["machines"] = machines

    # Index by id for quick update
    by_id: Dict[int, Dict[str, Any]] = {}
    for m in machines:
        try:
            mid = int(m.get("id")) if isinstance(m, dict) else int(getattr(m, "id", 0))
        except Exception:
            continue
        if mid:
            by_id[mid] = m  # keep original dict reference

    # Apply toggles; create entry if missing (relay defaults to id-1)
    for mid, enabled in toggles.items():
        if mid in by_id:
            if isinstance(by_id[mid], dict):
                by_id[mid]["enabled"] = enabled
            else:
                # unexpected object; replace with dict
                by_id[mid] = {"id": mid, "relay": mid - 1, "enabled": enabled}
        else:
            machines.append({"id": mid, "relay": mid - 1, "enabled": enabled})


def _normalize_sources(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize raw dict to AppConfig-compatible keys regardless of source style."""
    norm = dict(raw)

    # Map YAML style 'machines' dict -> machines_yaml
    if isinstance(norm.get("machines"), dict):
        norm["machines_yaml"] = norm.get("machines")
        norm["machines"] = None

    # Promote nested YAML blocks
    if isinstance(norm.get("adam"), dict):
        a = norm["adam"]
        norm.setdefault("adam_host", a.get("host"))
        norm.setdefault("adam_port", a.get("port"))
        norm.setdefault("adam_unit_id", a.get("unit_id", 1))
    if isinstance(norm.get("ha"), dict):
        h = norm["ha"]
        norm.setdefault("ha_url", h.get("base_url"))
        norm.setdefault("ha_token", h.get("token"))
        norm.setdefault("tts_service", h.get("tts_service"))
        norm.setdefault("media_player", h.get("media_player"))

    # Merge top-level machine toggles into machines list
    _apply_machine_toggles(norm)

    return norm


def _log_summary(cfg: AppConfig, source: str) -> None:
    try:
        if cfg.machines is not None:
            m = ",".join(f"{x.id}:{'1' if x.enabled else '0'}" for x in cfg.machines)
        else:
            m = ",".join(sorted(cfg.machines_yaml.keys())) if cfg.machines_yaml else "none"
    except Exception:
        m = "unknown"

    logger.info("[WMPS] Configuration loaded from %s", source)
    logger.info("[WMPS] Summary: simulate=%s, price_per_cycle=%s, machines=%s", cfg.simulate, cfg.price_per_cycle, m or "none")
    logger.info("[WMPS] Files: accounts=%s, transactions=%s", cfg.accounts_file, cfg.transactions_file)


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
