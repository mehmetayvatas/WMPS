"""
Keypad session state machine.
- Collects digits, enforces timeouts and lockouts, and counts failed attempts.
- Designed to be embedded in a FastAPI app or a background loop.
- All timings are wall-clock based (monotonic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
import time


@dataclass
class SecurityPolicy:
    """Security policy parameters for keypad code entry."""
    code_entry_timeout_s: int = 30
    max_failed_attempts: int = 5
    lockout_seconds: int = 120
    code_length: int = 4  # expected PIN length


@dataclass
class KeypadSession:
    """
    Keypad session manager.
    - Accepts digit input and manages the current buffer.
    - Applies timeout between key presses.
    - Tracks failed attempts and enforces a lockout window.
    - Calls a verifier to validate the final code.
    """
    policy: SecurityPolicy = field(default_factory=SecurityPolicy)
    verify_fn: Optional[Callable[[str], bool]] = None  # returns True if code is valid

    # runtime
    _buffer: str = ""
    _last_key_ts: float = 0.0
    _failed_attempts: int = 0
    _lockout_until: float = 0.0

    def now(self) -> float:
        """Return monotonic time for robust timing/timeout behavior."""
        return time.monotonic()

    # -------------------- Lockout / timeout helpers --------------------
    def is_locked(self) -> bool:
        """Return True if the session is currently locked out."""
        return self.now() < self._lockout_until

    def remaining_lockout(self) -> int:
        """Return remaining lockout time in whole seconds."""
        if not self.is_locked():
            return 0
        return int(round(self._lockout_until - self.now()))

    def _bump_lockout(self) -> None:
        """Enter lockout state according to policy."""
        self._lockout_until = self.now() + max(1, int(self.policy.lockout_seconds))
        self._failed_attempts = 0  # reset counter after entering lockout
        self._buffer = ""

    def _reset_timeout_if_needed(self) -> None:
        """Clear buffer if the inter-key timeout has expired."""
        if self._buffer and (self.now() - self._last_key_ts) > self.policy.code_entry_timeout_s:
            self._buffer = ""

    # -------------------- Public API --------------------
    def input_digit(self, d: str) -> dict:
        """
        Push a single digit (0-9). Returns a dict with the new state:
        {
          "status": "collecting" | "accepted" | "rejected" | "locked",
          "buffer": "12",
          "failed_attempts": 1,
          "remaining_lockout_s": 0
        }
        """
        if self.is_locked():
            return {
                "status": "locked",
                "buffer": "",
                "failed_attempts": self._failed_attempts,
                "remaining_lockout_s": self.remaining_lockout(),
            }

        if not (len(d) == 1 and d.isdigit()):
            return {
                "status": "rejected",
                "buffer": self._buffer,
                "failed_attempts": self._failed_attempts,
                "remaining_lockout_s": 0,
            }

        # timeout check
        self._reset_timeout_if_needed()

        # append
        self._buffer += d
        self._last_key_ts = self.now()

        # still collecting
        if len(self._buffer) < self.policy.code_length:
            return {
                "status": "collecting",
                "buffer": self._buffer,
                "failed_attempts": self._failed_attempts,
                "remaining_lockout_s": 0,
            }

        # reached code length -> validate
        code = self._buffer
        self._buffer = ""  # clear input buffer post-validation
        ok = bool(self.verify_fn(code)) if self.verify_fn else False

        if ok:
            self._failed_attempts = 0
            return {
                "status": "accepted",
                "buffer": "",
                "failed_attempts": 0,
                "remaining_lockout_s": 0,
            }

        # failed
        self._failed_attempts += 1
        if self._failed_attempts >= self.policy.max_failed_attempts:
            self._bump_lockout()
            return {
                "status": "locked",
                "buffer": "",
                "failed_attempts": 0,
                "remaining_lockout_s": self.remaining_lockout(),
            }

        return {
            "status": "rejected",
            "buffer": "",
            "failed_attempts": self._failed_attempts,
            "remaining_lockout_s": 0,
        }

    def backspace(self) -> dict:
        """Remove last digit from the buffer (if any)."""
        if self.is_locked():
            return {
                "status": "locked",
                "buffer": "",
                "failed_attempts": self._failed_attempts,
                "remaining_lockout_s": self.remaining_lockout(),
            }
        self._reset_timeout_if_needed()
        self._buffer = self._buffer[:-1] if self._buffer else ""
        return {
            "status": "collecting",
            "buffer": self._buffer,
            "failed_attempts": self._failed_attempts,
            "remaining_lockout_s": 0,
        }

    def reset(self) -> None:
        """Reset session state: buffer and failed attempts (no lockout change)."""
        self._buffer = ""
        self._failed_attempts = 0

