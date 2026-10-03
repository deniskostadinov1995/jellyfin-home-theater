"""
find_wled.py — Jellyfin Home Theater helper

Finds the WLED controllers on your network and prints the line to paste into
config.py.  Useful because WLED boxes usually sit on DHCP, so their addresses
move whenever the router hands out new leases.

    python find_wled.py

It sweeps your own subnet asking each address for /json/info and keeps the
replies that identify themselves as WLED.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import concurrent.futures
import json
import socket
import sys
import urllib.request


def local_subnet():
    """The /24 this PC is on, e.g. "192.168.0"."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just asks the OS which interface would be
        # used to reach the internet, which is the LAN we care about.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()


def probe(ip, timeout=1.2):
    try:
        with urllib.request.urlopen(f"http://{ip}/json/info", timeout=timeout) as r:
            info = json.load(r)
        if info.get("brand") == "WLED":
            return ip, info
    except Exception:
        pass
    return None


def main():
    subnet = sys.argv[1] if len(sys.argv) > 1 else local_subnet()
    print(f"Scanning {subnet}.1-254 for WLED controllers…\n")

    targets = [f"{subnet}.{n}" for n in range(1, 255)]
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe, targets):
            if result:
                found.append(result)

    if not found:
        print("No WLED controllers answered.")
        print("Check they are powered on and joined to this network, then try")
        print("again — or pass a subnet explicitly:  python find_wled.py 192.168.1")
        return

    print(f"Found {len(found)} controller(s):\n")
    for ip, info in found:
        leds = info.get("leds") or {}
        live = " — currently driven by a realtime source (e.g. SignalRGB)" \
               if info.get("live") else ""
        print(f"  {ip:<16} {info.get('name', '?'):<22} "
              f"WLED {info.get('ver', '?')}, {leds.get('count', '?')} LEDs{live}")

    addresses = ", ".join(f'"{ip}"' for ip, _ in found)
    print("\nPaste this into config.py:\n")
    print(f"WLED_IP = [{addresses}]")


if __name__ == "__main__":
    main()
