"""
find_displays.py — Jellyfin Home Theater helper
────────────────────────────────────────────
Run this script to list every display Windows currently sees.
Copy the Name of the display you want to watch into MONITOR_NAME in config.py.

Usage:
    python find_displays.py
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import ctypes


def list_displays_screeninfo():
    from screeninfo import get_monitors
    monitors = get_monitors()
    print(f"Found {len(monitors)} display(s) via screeninfo:\n")
    for i, m in enumerate(monitors, 1):
        primary = "  ← PRIMARY" if m.is_primary else ""
        print(f"  [{i}] Name     : {m.name}{primary}")
        print(f"      Position : ({m.x}, {m.y})")
        print(f"      Size     : {m.width} × {m.height} px")
        print()


def list_displays_win32():
    """Fallback using raw Win32 ctypes — no extra packages needed."""

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEX(ctypes.Structure):
        MONITORINFOF_PRIMARY = 0x00000001
        _fields_ = [
            ("cbSize",    ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork",    RECT),
            ("dwFlags",   ctypes.c_ulong),
            ("szDevice",  ctypes.c_wchar * 32),
        ]

    monitors = []
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_long), ctypes.c_double,
    )
    user32 = ctypes.windll.user32

    def _cb(hMon, _hdc, _lprc, _data):
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hMon, ctypes.byref(info)):
            r = info.rcMonitor
            monitors.append({
                "name":       info.szDevice,
                "x":          r.left,
                "y":          r.top,
                "width":      r.right  - r.left,
                "height":     r.bottom - r.top,
                "primary":    bool(info.dwFlags & MONITORINFOEX.MONITORINFOF_PRIMARY),
            })
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)

    print(f"Found {len(monitors)} display(s) via Win32 API:\n")
    for i, m in enumerate(monitors, 1):
        primary = "  ← PRIMARY" if m["primary"] else ""
        print(f"  [{i}] Name     : {m['name']}{primary}")
        print(f"      Position : ({m['x']}, {m['y']})")
        print(f"      Size     : {m['width']} × {m['height']} px")
        print()


if __name__ == "__main__":
    print("=" * 50)
    print("  Jellyfin Home Theater — Connected Displays")
    print("=" * 50)
    print()
    try:
        list_displays_screeninfo()
    except ImportError:
        print("(screeninfo not installed — using Win32 API fallback)\n")
        list_displays_win32()

    print("─" * 50)
    print("Copy the Name into MONITOR_NAME in config.py.")
    print('Example:  MONITOR_NAME = r"\\\\.\\ DISPLAY3"')
    print("─" * 50)
