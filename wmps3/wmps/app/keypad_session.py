"""
Keypad session state machine.
- Collects digits, enforces timeouts and lockouts, supports confirm/clear/backspace.
- Designed to be embedded in a FastAPI app or a background loop.
- All timings are wall-clock based (monotonic).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Any
import time


@dataclass
class SecurityPolicy:
    """Security policy parameters for keypad code entry."""
    code_entry_timeout_s: int = 30
    max_failed_attempts: int = 5
    lockout_seconds: int = 120
    code_length: int = 4            # expected PIN length
    require_confirm: bool = True    # validate only on confirm key (e.g. '#')
    confirm_key: str = "#"
    clear_key: str = "*"
    backspace_key: str = "B"        # you may map 'B' to a hardware key (e.g., Backspace)


@dataclass
class KeypadSession:
    """
    Keypad session manager.
    - Accepts digit/control input and manages the current buffer.
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

    # ------------- time helpers -------------
    def now(self) -> float:
        """Return monotonic time for robust timing/timeout behavior."""
        return time.monotonic()

    # ------------- lockout / timeout -------------
    def is_locked(self) -> bool:
        """Return True if the session is currently locked out."""
        return self.now() < self._lockout_until

    def remaining_lockout(self) -> int:
        """Return remaining lockout time in whole seconds."""
        if not self.is_locked():
            return 0
        return max(0, int(round(self._lockout_until - self.now())))

    def _enter_lockout(self) -> None:
        """Enter lockout state according to policy."""
        self._lockout_until = self.now() + max(1, int(self.policy.lockout_seconds))
        self._failed_attempts = 0  # reset counter after entering lockout
        self._buffer = ""

    def _apply_interkey_timeout(self) -> None:
        """Clear buffer if the inter-key timeout has expired."""
        if self._buffer and (self.now() - self._last_key_ts) > self.policy.code_entry_timeout_s:
            self._buffer = ""

    # ------------- public API -------------
    def feed(self, ch: str) -> Dict[str, Any]:
        """
        Feed a single character (digit or control).
        Returns a dict with the new state:
        {
          "status": "collecting" | "accepted" | "rejected" | "locked" | "cleared" | "incomplete",
          "event": "digit" | "confirm" | "clear" | "backspace" | "invalid",
          "buffer": "12",
          "failed_attempts": 1,
          "remaining_lockout_s": 0,
          "accepted_code": "1234"   # only present if status == "accepted"
        }
        """
        if self.is_locked():
            return self._resp("locked", "invalid")

        if not ch or len(ch) != 1:
            return self._resp("rejected", "invalid")

        # Apply inter-key timeout before mutating the buffer
        self._apply_interkey_timeout()

        # Control keys
        if ch == self.policy.clear_key:
            self._buffer = ""
            return self._resp("cleared", "clear")

        if ch == self.policy.backspace_key:
            self._buffer = self._buffer[:-1] if self._buffer else ""
            self._last_key_ts = self.now()
            return self._resp("collecting", "backspace")

        if ch == self.policy.confirm_key:
            return self._on_confirm()

        # Digits
        if ch.isdigit():
            # Append, but cap at code_length to avoid unbounded growth
            if len(self._buffer) < self.policy.code_length:
                self._buffer += ch
            # Stamp time even if capped (user feedback stays responsive)
            self._last_key_ts = self.now()

            if self.policy.require_confirm:
                # Wait for confirm key to validate
                return self._resp("collecting", "digit")
            else:
                # Auto-validate when reaching exact length
                if len(self._buffer) < self.policy.code_length:
                    return self._resp("collecting", "digit")
                # length reached -> validate immediately
                code = self._buffer
                self._buffer = ""
                return self._validate(code)

        # Unknown char
        return self._resp("rejected", "invalid")

    # ------------- helpers -------------
    def _on_confirm(self) -> Dict[str, Any]:
        """Validate current buffer if confirmation is requested."""
        # If require_confirm is False, confirm acts as "try validate if length matches"
        if not self._buffer:
            return self._resp("incomplete", "confirm")  # nothing to validate

        if len(self._buffer) != self.policy.code_length:
            # Not enough digits yet
            return self._resp("incomplete", "confirm")

        code = self._buffer
        self._buffer = ""
        self._last_key_ts = self.now()
        return self._validate(code)

    def _validate(self, code: str) -> Dict[str, Any]:
        ok = bool(self.verify_fn(code)) if self.verify_fn else False
        if ok:
            self._failed_attempts = 0
            return self._resp("accepted", "confirm" if self.policy.require_confirm else "digit", accepted_code=code)

        # failed
        self._failed_attempts += 1
        if self._failed_attempts >= self.policy.max_failed_attempts:
            self._enter_lockout()
            return self._resp("locked", "confirm" if self.policy.require_confirm else "digit")

        return self._resp("rejected", "confirm" if self.policy.require_confirm else "digit")

    def backspace(self) -> Dict[str, Any]:
        """Explicit backspace helper (equivalent to feeding backspace_key)."""
        return self.feed(self.policy.backspace_key)

    def clear(self) -> Dict[str, Any]:
        """Explicit clear helper (equivalent to feeding clear_key)."""
        return self.feed(self.policy.clear_key)

    def reset(self) -> None:
        """Reset session state: buffer and failed attempts (no lockout change)."""
        self._buffer = ""
        self._failed_attempts = 0

    # ------------- response builder -------------
    def _resp(self, status: str, event: str, *, accepted_code: Optional[str] = None) -> Dict[str, Any]:
        resp: Dict[str, Any] = {
            "status": status,
            "event": event,
            "buffer": self._buffer,
            "failed_attempts": self._failed_attempts,
            "remaining_lockout_s": self.remaining_lockout(),
        }
        if accepted_code is not None:
            resp["accepted_code"] = accepted_code
        return resp
