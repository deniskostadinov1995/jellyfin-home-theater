"""
find_ha_lights.py — Jellyfin Home Theater helper

Lists the lights Home Assistant knows about and prints the config line for the
ones that look like living-room ceiling lights, so you can paste it into
HA_LIGHTS in config.py.

    python find_ha_lights.py

Needs HA_URL and HA_TOKEN set in config.py first.  To make a token: open Home
Assistant, click your user at the bottom left, Security, then "Long-lived
access tokens" → Create token.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import sys

from homeassistant import HomeAssistantClient, HomeAssistantError

try:
    from config import HA_URL, HA_TOKEN
except ImportError:
    print("config.py has no HA_URL / HA_TOKEN. Add them first.")
    sys.exit(1)

# Words that suggest a ceiling light in a living room, for the suggestion at
# the end. Only a hint — the full list is printed regardless.
LIVING_ROOM_HINTS = ("living", "lounge", "salon", "hol", "vsekidnevna")
CEILING_HINTS = ("ceiling", "plafon", "spot", "downlight", "main", "overhead")


def main():
    client = HomeAssistantClient(HA_URL, HA_TOKEN)
    if not client.configured:
        print("HA_TOKEN is empty in config.py — nothing to query yet.")
        print("Home Assistant → your user (bottom left) → Security →")
        print("Long-lived access tokens → Create token.")
        return

    try:
        print(f"Connected to Home Assistant at {HA_URL}: {client.ping()}\n")
        lights = client.light_entities()
    except HomeAssistantError as exc:
        print(exc)
        return
    except Exception as exc:
        print(f"Could not reach Home Assistant at {HA_URL}: {exc}")
        return

    if not lights:
        print("Home Assistant reported no light.* entities.")
        return

    print(f"{len(lights)} light(s):\n")
    width = max(len(l["entity_id"]) for l in lights)
    for l in lights:
        bri = l["brightness"]
        level = f"{round((bri or 0) / 255 * 100)}%" if l["on"] and bri else \
                ("on" if l["on"] else "off")
        print(f"  {l['entity_id']:<{width}}  {l['name']:<28} "
              f"{level:<5} {'dimmable' if l['dimmable'] else 'on/off only'}")

    suggested = [l["entity_id"] for l in lights
                 if any(h in l["entity_id"].lower() or h in l["name"].lower()
                        for h in LIVING_ROOM_HINTS)
                 and any(h in l["entity_id"].lower() or h in l["name"].lower()
                         for h in CEILING_HINTS)]
    if not suggested:
        suggested = [l["entity_id"] for l in lights
                     if any(h in l["entity_id"].lower() or h in l["name"].lower()
                            for h in LIVING_ROOM_HINTS)]

    print("\nPaste the ones you want dimmed into config.py:\n")
    if suggested:
        print("HA_LIGHTS = [" + ", ".join(f'"{e}"' for e in suggested) + "]")
        print("\n(guessed from their names — adjust if that's not the right set)")
    else:
        print('HA_LIGHTS = ["light.your_ceiling_light"]')


if __name__ == "__main__":
    main()
