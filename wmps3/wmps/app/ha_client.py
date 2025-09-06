from __future__ import annotations
from typing import Optional, Dict, Any, Tuple
import httpx


class HAClient:
    """
    Lightweight Home Assistant HTTP client (services only).

    Features:
    - Generic service call: call_service(domain, service, data)
    - TTS helper: speak(...), works with both 'tts.speak' (new) and 'tts.*_say' (legacy)
    - Ideal for VLC Telnet: tts_service='tts.google_translate_say', media_player='media_player.vlc_telnet'
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 5.0):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)

    # ----------------- internal helpers -----------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _svc_url(self, domain: str, service: str) -> str:
        if not self.base_url:
            raise ValueError("HA base_url is empty.")
        return f"{self.base_url}/api/services/{domain}/{service}"

    @staticmethod
    def _parse_service(
        tts_service: Optional[str],
        default_domain: str = "tts",
        default_service: str = "speak",
    ) -> Tuple[str, str]:
        """
        'tts.google_translate_say'  → ('tts','google_translate_say')
        'speak'                      → ('tts','speak')
        None                         → ('tts','speak')
        """
        if tts_service:
            if "." in tts_service:
                d, s = tts_service.split(".", 1)
                return (d or default_domain), (s or default_service)
            return default_domain, tts_service
        return default_domain, default_service

    # ----------------- public: generic service -----------------
    async def call_service(self, domain: str, service: str, data: Dict[str, Any]) -> Optional[dict]:
        """
        Call any Home Assistant service; returns JSON or empty dict.
        Returns None if base_url/token are missing.
        Raises for HTTP errors.
        """
        if not (self.base_url and self.token):
            return None
        url = self._svc_url(domain, service)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=self._headers(), json=data)
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}

    # ----------------- public: TTS helper -----------------
    async def speak(
        self,
        message: str,
        *,
        media_player: Optional[str] = None,
        tts_service: Optional[str] = None,  # e.g., "tts.google_translate_say" or "tts.speak"
        tts_entity: Optional[str] = None,   # for tts.speak: specific TTS entity (e.g., "tts.cloud_say")
        cache: Optional[bool] = None
    ) -> Optional[dict]:
        """
        Speak a message via Home Assistant TTS.

        - For 'tts.speak' (new API):
            payload = {
              "message": "...",
              "media_player_entity_id": "<media_player>",
              "entity_id": "<tts_entity>",  # optional
              "cache": false                # optional
            }

        - For legacy 'tts.*_say':
            payload = {
              "entity_id": "<media_player>",
              "message": "...",
              "cache": false                # optional
            }
        """
        domain, service = self._parse_service(tts_service, "tts", "speak")
        payload: Dict[str, Any] = {"message": message}

        if domain == "tts" and service == "speak":
            if media_player:
                payload["media_player_entity_id"] = media_player
            if tts_entity:
                payload["entity_id"] = tts_entity
            if cache is not None:
                payload["cache"] = bool(cache)
        else:
            if media_player:
                payload["entity_id"] = media_player
            if cache is not None:
                payload["cache"] = bool(cache)

        return await self.call_service(domain, service, payload)

    # ----------------- convenience: switches -----------------
    async def turn_on(self, entity_id: str) -> Optional[dict]:
        return await self.call_service("switch", "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> Optional[dict]:
        return await self.call_service("switch", "turn_off", {"entity_id": entity_id})
