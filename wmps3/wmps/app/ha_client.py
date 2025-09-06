from __future__ import annotations
import httpx
from typing import Optional

class HAClient:
    """Lightweight Home Assistant HTTP client (services only)."""

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.token = token or ""

    async def speak(self, entity_id: str, message: str,
                    service_domain: str = "tts", service: str = "speak") -> Optional[dict]:
        """Call Home Assistant service to speak a message."""
        if not (self.base_url and self.token):
            return None
        url = f"{self.base_url}/api/services/{service_domain}/{service}"
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        payload = {"entity_id": entity_id, "message": message}
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.json()
