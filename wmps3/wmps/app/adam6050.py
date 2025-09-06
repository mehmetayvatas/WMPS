"""
ADAM-6050 I/O module handler (with logging)
- Controls digital outputs (DO) and reads digital inputs (DI) via Modbus TCP.
- Supports a full mock mode for local testing without hardware.
- Default mapping: machines 1..6 -> DO coils 16..21, DI inputs 0..5.
"""

from __future__ import annotations

import time
from typing import List, Optional

from app.utils import logger

# Optional pymodbus import (guarded)
try:
    from pymodbus.client import ModbusTcpClient  # type: ignore
    _HAS_PYMODBUS = True
except Exception:
    ModbusTcpClient = object  # type: ignore
    _HAS_PYMODBUS = False


class Adam6050:
    def __init__(
        self,
        host: str = "192.168.1.101",
        port: int = 502,
        *,
        enabled: bool = True,
        unit_id: int = 1,
        coils: Optional[List[int]] = None,
        inputs: Optional[List[int]] = None,
        mock_confirm_after_pulse: bool = True,
        mock_confirm_delay_s: float = 0.2,
    ) -> None:
        """
        Initialize ADAM-6050 handler.

        :param host: ADAM-6050 IP address
        :param port: Modbus TCP port (default 502)
        :param enabled: If False, works in mock mode (no hardware needed)
        :param unit_id: Modbus unit/slave id
        :param coils: DO coil addresses for machines 1..6 (len=6). Default [16..21]
        :param inputs: DI addresses for machines 1..6 (len=6). Default [0..5]
        :param mock_confirm_after_pulse: In mock mode, mark DI as active after pulse
        :param mock_confirm_delay_s: In mock mode, wait before DI becomes active
        """
        self.host = host
        self.port = port
        self.enabled = enabled
        self.unit_id = unit_id
        self.coils = coils or [16, 17, 18, 19, 20, 21]
        self.inputs = inputs or [0, 1, 2, 3, 4, 5]

        # Mock-state
        self._mock_confirm_after_pulse = mock_confirm_after_pulse
        self._mock_confirm_delay_s = float(mock_confirm_delay_s)
        self._mock_active: set[int] = set()

        if len(self.coils) != 6 or len(self.inputs) != 6:
            raise ValueError("coils and inputs must each contain 6 items for machines 1..6")

        mode = "REAL" if self.enabled else "MOCK"
        logger.info(f"[ADAM6050] Initialized ({mode}) host={self.host}:{self.port}, unit_id={self.unit_id}")

        if self.enabled and not _HAS_PYMODBUS:
            logger.warning("[ADAM6050] pymodbus not installed; real I/O will not function")

    # -----------------------
    # Public API
    # -----------------------
    def pulse_relay(self, machine_number: int, pulse_seconds: int = 2) -> bool:
        """
        Pulse the relay (DO) for a given machine.
        In mock mode, simulates success and (optionally) sets DI active after a short delay.

        :param machine_number: 1..6
        :param pulse_seconds: how long to keep DO ON before turning OFF (seconds)
        :return: True if write operations were successful (or simulated success in mock)
        """
        self._validate_machine_index(machine_number)

        if not self.enabled:
            logger.info(f"[ADAM6050][MOCK] Pulse relay m#{machine_number} for {pulse_seconds}s")
            if self._mock_confirm_after_pulse:
                time.sleep(min(self._mock_confirm_delay_s, 0.5))
                self._mock_active.add(machine_number)
                logger.info(f"[ADAM6050][MOCK] DI set active for m#{machine_number}")
            return True

        if not _HAS_PYMODBUS:
            logger.warning("[ADAM6050] pymodbus missing; pretending success for pulse")
            return True

        coil_addr = self._coil(machine_number)
        try:
            with ModbusTcpClient(host=self.host, port=self.port) as client:  # type: ignore
                if not client.connect():
                    logger.error("[ADAM6050] Modbus connect failed")
                    return False

                logger.info(f"[ADAM6050] write_coil ON addr={coil_addr} (m#{machine_number})")
                wr_on = client.write_coil(coil_addr, True, slave=self.unit_id)
                if getattr(wr_on, "isError", lambda: True)():
                    logger.error("[ADAM6050] write_coil ON failed")
                    return False

                time.sleep(max(0.05, float(pulse_seconds)))

                logger.info(f"[ADAM6050] write_coil OFF addr={coil_addr} (m#{machine_number})")
                wr_off = client.write_coil(coil_addr, False, slave=self.unit_id)
                if getattr(wr_off, "isError", lambda: True)():
                    logger.error("[ADAM6050] write_coil OFF failed")
                    return False

                logger.info(f"[ADAM6050] Pulse complete m#{machine_number}")
                return True
        except Exception as e:
            logger.error(f"[ADAM6050] Exception during pulse m#{machine_number}: {e}")
            return False

    def is_machine_active(self, machine_number: int) -> bool:
        """
        Read the machine DI (running/active).
        In mock mode, returns from the internal mock set.

        :param machine_number: 1..6
        :return: True if DI is active
        """
        self._validate_machine_index(machine_number)

        if not self.enabled:
            active = machine_number in self._mock_active
            logger.info(f"[ADAM6050][MOCK] Check active m#{machine_number} -> {active}")
            return active

        if not _HAS_PYMODBUS:
            logger.warning("[ADAM6050] pymodbus missing; assuming inactive")
            return False

        di_addr = self._input(machine_number)
        try:
            with ModbusTcpClient(host=self.host, port=self.port) as client:  # type: ignore
                if not client.connect():
                    logger.error("[ADAM6050] Modbus connect failed for DI read")
                    return False

                rr = client.read_discrete_inputs(address=di_addr, count=1, slave=self.unit_id)
                if getattr(rr, "isError", lambda: True)():
                    logger.error("[ADAM6050] read_discrete_inputs failed")
                    return False

                bits = getattr(rr, "bits", [False])
                active = bool(bits[0] if bits else False)
                logger.info(f"[ADAM6050] DI read addr={di_addr} (m#{machine_number}) -> {active}")
                return active
        except Exception as e:
            logger.error(f"[ADAM6050] Exception during DI read m#{machine_number}: {e}")
            return False

    # -----------------------
    # Mock control helpers
    # -----------------------
    def mock_set_active(self, machine_number: int, active: bool) -> None:
        """Manually override mock DI for a machine (useful in tests)."""
        self._validate_machine_index(machine_number)
        if active:
            self._mock_active.add(machine_number)
        else:
            self._mock_active.discard(machine_number)
        logger.info(f"[ADAM6050][MOCK] Force DI m#{machine_number} -> {active}")

    def mock_clear_all(self) -> None:
        """Clear all mock DI states."""
        self._mock_active.clear()
        logger.info("[ADAM6050][MOCK] Clear all DI states")

    # -----------------------
    # Internals
    # -----------------------
    def _validate_machine_index(self, machine_number: int) -> None:
        if not (1 <= machine_number <= 6):
            raise ValueError("machine_number must be in range 1..6")

    def _coil(self, machine_number: int) -> int:
        return self.coils[machine_number - 1]

    def _input(self, machine_number: int) -> int:
        return self.inputs[machine_number - 1]
