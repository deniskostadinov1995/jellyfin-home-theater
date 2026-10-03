"""
Jellyfin Home Theater — Home Assistant client.

Just enough of the REST API to read a light's state and change it, so the
playback automation can reach lights that aren't WLED (the living-room ceiling,
for instance).

Brightness is quietly inconsistent in this area, so this module fixes a
convention: everything crossing in or out of here is **0–255**, matching WLED
and Home Assistant's own `brightness` attribute. Percentages are only used at
the very edge, where `brightness_pct` is handed to the service call.
"""

import logging

import requests

log = logging.getLogger("hass")


class HomeAssistantError(RuntimeError):
    pass


def pct_to_255(pct):
    """A percentage (0–100) → Home Assistant's 0–255 brightness."""
    try:
        pct = max(0, min(100, float(pct)))
    except (TypeError, ValueError):
        return 0
    return int(round(pct * 255 / 100))


class HomeAssistantClient:
    """Talks to one Home Assistant instance with a long-lived access token."""

    def __init__(self, base_url, token):
        self.base = (base_url or "").rstrip("/")
        self.token = (token or "").strip()
        self.session = requests.Session()

    @property
    def configured(self):
        return bool(self.base and self.token)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    def request(self, method, path, json_body=None, timeout=8):
        if not self.configured:
            raise HomeAssistantError("Home Assistant is not configured")
        r = self.session.request(method, f"{self.base}/api{path}",
                                 headers=self._headers(), json=json_body,
                                 timeout=timeout)
        if r.status_code == 401:
            raise HomeAssistantError(
                "Home Assistant rejected the token. Create a new long-lived "
                "access token (your profile → Security) and put it in "
                "HA_TOKEN in config.py."
            )
        r.raise_for_status()
        return r

    def ping(self):
        """Confirm the URL and token work. Returns the API's greeting."""
        return (self.request("GET", "/").json() or {}).get("message", "")

    # ── Lights ──────────────────────────────────────────────────────────────
    def states(self):
        return self.request("GET", "/states").json() or []

    def light_entities(self):
        """Every `light.*` entity, with the bits needed to show and restore it."""
        out = []
        for s in self.states():
            entity_id = s.get("entity_id", "")
            if not entity_id.startswith("light."):
                continue
            attrs = s.get("attributes") or {}
            out.append({
                "entity_id":  entity_id,
                "name":       attrs.get("friendly_name") or entity_id,
                "state":      s.get("state"),
                "on":         s.get("state") == "on",
                "brightness": attrs.get("brightness"),
                # Not every light dims; turning brightness on a switch-like
                # light is harmless but pointless, and worth knowing about.
                "dimmable":   "brightness" in (attrs.get("supported_color_modes") or [])
                              or attrs.get("brightness") is not None,
            })
        out.sort(key=lambda e: e["name"].lower())
        return out

    def light_state(self, entity_id):
        s = self.request("GET", f"/states/{entity_id}").json() or {}
        attrs = s.get("attributes") or {}
        return {"entity_id": entity_id,
                "name": attrs.get("friendly_name") or entity_id,
                "on": s.get("state") == "on",
                "brightness": attrs.get("brightness")}

    def call(self, domain, service, data):
        return self.request("POST", f"/services/{domain}/{service}",
                            json_body=data)

    def set_light(self, entity_id, on, brightness=None):
        """Switch a light, optionally to a given 0–255 brightness."""
        if not on:
            return self.call("light", "turn_off", {"entity_id": entity_id})
        data = {"entity_id": entity_id}
        if brightness is not None:
            data["brightness"] = max(1, min(255, int(brightness)))
        return self.call("light", "turn_on", data)
