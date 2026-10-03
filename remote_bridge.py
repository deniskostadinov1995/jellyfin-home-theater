"""
Jellyfin Home Theater — USB Remote Bridge
=========================================
Makes the USB remote drive the TV UI no matter which window has focus on the
PC, while your keyboard carries on doing whatever you were doing.

The problem it solves: a USB remote is just an HID keyboard, so Windows sends
its arrow keys to the foreground window. If you are working on the PC, the
remote drives *that* instead of Jellyfin — and the only fix was to walk over
and click the kiosk window.

How it works:

  1. **Raw Input** (registered with `RIDEV_INPUTSINK`) reports which physical
     device each keystroke came from, and arrives even though this process has
     no focused window. That is how the remote is told apart from the keyboard.
  2. A **low-level keyboard hook** swallows the remote's keys so they never
     reach whatever you are using. Raw Input cannot do this; the hook can.
  3. The keypress is forwarded to the TV UI as a command over HTTP — the same
     channel the phone remote uses. The kiosk window never needs focus.

Your keyboard is untouched throughout: only keystrokes from the configured
remote are intercepted.

    python remote_probe.py      # once: find out which device the remote is
    python remote_bridge.py     # run it
    python remote_bridge.py --install     # start at logon (undo: --uninstall)

It stays out of the way when there is nothing to control: with no TV page open,
keys are passed straight through and the remote behaves like an ordinary
keyboard again.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────────────
try:
    from config import PORT
except ImportError:
    PORT = 5050
try:
    from config import REMOTE_DEVICE
except ImportError:
    REMOTE_DEVICE = ""
try:
    from config import REMOTE_KEYS
except ImportError:
    REMOTE_KEYS = {}
try:
    from config import REMOTE_ONLY_WHEN_TV_RUNNING
except ImportError:
    REMOTE_ONLY_WHEN_TV_RUNNING = True
try:
    from config import REMOTE_TOGGLE_KEY
except ImportError:
    REMOTE_TOGGLE_KEY = None

APP = f"http://127.0.0.1:{PORT}"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(PROJECT_DIR, "remote_bridge.log")

# What each remote button should do, when config.py doesn't say otherwise.
# Names come from VK_NAMES below; the values are the actions the TV already
# understands (see handleCommand in static/js/tv.js).
DEFAULT_KEYS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Enter": "select",
    "Esc": "back", "Backspace": "back", "BrowserBack": "back",
    "Home": "home", "BrowserHome": "home",
    "MediaPlayPause": "toggle_play",
    "MediaStop": "stop",
    "MediaNext": "next", "MediaPrev": "previous",
    "VolumeUp": "vol_up", "VolumeDown": "vol_down", "VolumeMute": "mute",
}

# ── Win32 ────────────────────────────────────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDI_DEVICENAME = 0x20000007
HWND_MESSAGE = wintypes.HWND(-3)
_NO_WINDOW = 0x08000000


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


_LLPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)

# Declaring argtypes is not optional: without them ctypes narrows 64-bit
# handles and calls fail with "int too long to convert".
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LLPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.RegisterClassW.restype = wintypes.WORD
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE),
                                           wintypes.UINT, wintypes.UINT]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                          wintypes.LPVOID,
                                          ctypes.POINTER(wintypes.UINT)]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0xA6: "BrowserBack", 0xA7: "BrowserForward", 0xA8: "BrowserRefresh",
    0xA9: "BrowserStop", 0xAA: "BrowserSearch", 0xAB: "BrowserFavorites",
    0xAC: "BrowserHome",
    0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp",
    0xB0: "MediaNext", 0xB1: "MediaPrev", 0xB2: "MediaStop",
    0xB3: "MediaPlayPause", 0xB5: "LaunchMediaSelect",
    0xB6: "LaunchApp1", 0xB7: "LaunchApp2",
}
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
for _c in list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)):
    VK_NAMES[_c] = chr(_c)
NAME_TO_VK = {n.lower(): v for v, n in VK_NAMES.items()}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def build_keymap():
    """config.py's REMOTE_KEYS, merged over the defaults. Keys may be given as
    names ("MediaPlayPause") or raw codes (0xB3 / "0xB3")."""
    merged = dict(DEFAULT_KEYS)
    merged.update(REMOTE_KEYS or {})
    out = {}
    for key, action in merged.items():
        if action is None:
            continue                     # an explicit "leave this key alone"
        if isinstance(key, int):
            vk = key
        elif str(key).lower() in NAME_TO_VK:
            vk = NAME_TO_VK[str(key).lower()]
        else:
            try:
                vk = int(str(key), 0)
            except ValueError:
                log(f"config REMOTE_KEYS: don't recognise key {key!r} — ignored")
                continue
        out[vk] = action
    return out


KEYMAP = build_keymap()
TOGGLE_VK = None
if REMOTE_TOGGLE_KEY is not None:
    TOGGLE_VK = (REMOTE_TOGGLE_KEY if isinstance(REMOTE_TOGGLE_KEY, int)
                 else NAME_TO_VK.get(str(REMOTE_TOGGLE_KEY).lower()))


# ── Talking to the app ──────────────────────────────────────────────────────
_send_q = queue.Queue()
_tv = {"alive": False, "checked": 0.0}
_enabled = {"on": True}


def _post(path, body):
    req = urllib.request.Request(APP + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


def _sender():
    """Commands go out on their own thread.

    The keyboard hook must return in microseconds — Windows silently drops a
    hook that dawdles — so it only ever enqueues.
    """
    while True:
        action = _send_q.get()
        if action is None:
            return
        try:
            _post("/api/command", {"action": action})
        except Exception as exc:
            log(f"could not send {action}: {exc}")


def tv_alive():
    """Is a TV page open? Cached, because this is consulted per keypress."""
    now = time.time()
    if now - _tv["checked"] > 2.0:
        _tv["checked"] = now
        try:
            with urllib.request.urlopen(APP + "/api/tv_status", timeout=2) as r:
                _tv["alive"] = bool(json.load(r).get("connected"))
        except Exception:
            _tv["alive"] = False
    return _tv["alive"]


# ── Which device sent the key ───────────────────────────────────────────────
_device_names = {}          # raw handle -> "VID_xxxx&PID_xxxx"
_recent = {}                # vk -> (is_remote, timestamp), set by Raw Input
_last_source_is_remote = {"v": False}
_RECENT_TTL = 0.25


def device_id(handle):
    if handle in _device_names:
        return _device_names[handle]
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    name = ""
    if size.value:
        buf = ctypes.create_unicode_buffer(size.value)
        user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf, ctypes.byref(size))
        name = buf.value
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", name or "")
    ident = f"VID_{m.group(1).upper()}&PID_{m.group(2).upper()}" if m else (name or "?")
    _device_names[handle] = ident
    return ident


def is_remote_device(ident):
    want = (REMOTE_DEVICE or "").strip().upper()
    return bool(want) and want in (ident or "").upper()


@_WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        size = wintypes.UINT(0)
        user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, None,
                               ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, buf,
                                   ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
            ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
            if ri.header.dwType == RIM_TYPEKEYBOARD:
                mine = is_remote_device(device_id(ri.header.hDevice))
                _recent[ri.keyboard.VKey] = (mine, time.perf_counter())
                _last_source_is_remote["v"] = mine
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def key_is_from_remote(vk):
    """Best available answer, right now, inside the hook.

    Raw Input usually lands just before the hook runs, in which case this is
    exact. When it hasn't yet, fall back on whichever device was used last —
    which is right for every key after the first of a burst, and a burst is
    what pressing a remote button looks like.
    """
    hit = _recent.get(vk)
    if hit and (time.perf_counter() - hit[1]) < _RECENT_TTL:
        return hit[0]
    return _last_source_is_remote["v"]


# ── The hook ────────────────────────────────────────────────────────────────
HOOK_SWALLOW = 1


@_LLPROC
def _ll_proc(nCode, wParam, lParam):
    if nCode != 0:
        return user32.CallNextHookEx(None, nCode, wParam, lParam)
    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
    vk = kb.vkCode
    keydown = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)

    if not key_is_from_remote(vk):
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # From here on it is the remote, so the keyboard is never affected.
    if TOGGLE_VK is not None and vk == TOGGLE_VK:
        if keydown:
            _enabled["on"] = not _enabled["on"]
            log(f"bridge {'enabled' if _enabled['on'] else 'disabled'} (toggle key)")
        return HOOK_SWALLOW

    if not _enabled["on"]:
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    action = KEYMAP.get(vk)
    if action is None:
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    # Nothing listening? Let the key behave normally rather than vanish.
    if REMOTE_ONLY_WHEN_TV_RUNNING and not tv_alive():
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    if keydown:
        _send_q.put(action)
    # Swallow key-up too, or the foreground app sees a release it never saw
    # pressed — which some applications treat as a stuck key.
    return HOOK_SWALLOW


# ── Plumbing ────────────────────────────────────────────────────────────────
def make_window():
    cls = WNDCLASS()
    cls.lpfnWndProc = ctypes.cast(_wnd_proc, ctypes.c_void_p)
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.lpszClassName = "JHTRemoteBridge"
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, "JHTRemoteBridge", "bridge", 0, 0, 0, 0, 0,
                                  HWND_MESSAGE, None, cls.hInstance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)
    if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                          ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def run():
    if not REMOTE_DEVICE:
        log("REMOTE_DEVICE is not set in config.py.")
        log("Run  python remote_probe.py  and paste the line it prints.")
        return

    log(f"Bridging {REMOTE_DEVICE} → {APP}")
    log(f"  {len(KEYMAP)} keys mapped: " +
        ", ".join(f"{VK_NAMES.get(v, hex(v))}→{a}" for v, a in sorted(KEYMAP.items())))
    if REMOTE_ONLY_WHEN_TV_RUNNING:
        log("  keys pass through normally while no TV page is open")
    if TOGGLE_VK is not None:
        log(f"  {VK_NAMES.get(TOGGLE_VK, hex(TOGGLE_VK))} toggles bridging")

    make_window()
    threading.Thread(target=_sender, daemon=True, name="remoteSender").start()
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _ll_proc,
                                    kernel32.GetModuleHandleW(None), 0)
    if not hook:
        log(f"Could not install the keyboard hook: "
            f"{ctypes.WinError(ctypes.get_last_error())}")
        return

    msg = wintypes.MSG()
    try:
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            # Expire stale device hints so a long-idle key can't be attributed
            # to whatever happened minutes ago.
            now = time.perf_counter()
            for vk in [k for k, (_, t) in _recent.items() if now - t > 2.0]:
                _recent.pop(vk, None)
            time.sleep(0.004)
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(hook)
        _send_q.put(None)
        log("Stopped.")


# ── Auto-start at logon ─────────────────────────────────────────────────────
def _startup_dir():
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _lnk():
    return os.path.join(_startup_dir(), "JellyfinRemoteBridge.lnk")


def python_exe():
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return pyw if os.path.exists(pyw) else exe


def install_startup():
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';"
        "$s.Arguments='\"{script}\"';"
        "$s.WorkingDirectory='{wd}';"
        "$s.WindowStyle=7;"
        "$s.Save()"
    ).format(lnk=_lnk(), target=python_exe(),
             script=os.path.abspath(__file__), wd=PROJECT_DIR)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   creationflags=_NO_WINDOW)
    log(f"Installed to Startup: {_lnk()}")


def uninstall_startup():
    if os.path.exists(_lnk()):
        os.remove(_lnk())
        log("Removed from Startup.")
    else:
        log("Nothing to remove — no Startup shortcut found.")


def main():
    args = set(sys.argv[1:])
    if "--install" in args:
        install_startup()
    elif "--uninstall" in args:
        uninstall_startup()
    else:
        run()


if __name__ == "__main__":
    main()
