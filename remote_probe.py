"""
remote_probe.py — Jellyfin Home Theater helper

Works out how to tell your USB remote apart from your keyboard, so the remote
can drive the TV UI no matter which window happens to be focused on the PC.

    python remote_probe.py

Press every button on the remote, then press the same keys on your keyboard,
then Ctrl+C.  It prints which physical device each keypress came from and the
codes it sent, and finishes with the config block to paste into config.py.

Nothing is blocked or swallowed while this runs — it only watches.

How it works, and why both halves are needed:

  · Raw Input (registered with RIDEV_INPUTSINK) reports the *device* each
    keystroke came from and arrives even when this window isn't focused.  It
    cannot stop a keystroke reaching other applications.
  · A WH_KEYBOARD_LL hook *can* stop one, but its callback is not told which
    device is responsible.

So the probe also measures which of the two fires first for the same key.  If
Raw Input wins, the hook can look up the device and block precisely; if the
hook wins, the bridge has to fall back on remembering which device was last
used.  The summary says which applies here.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import ctypes
import sys
import time
from collections import defaultdict
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_KEYUP, WM_SYSKEYUP = 0x0101, 0x0105
WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDI_DEVICENAME = 0x20000007
HWND_MESSAGE = wintypes.HWND(-3)


# ── Win32 structures ────────────────────────────────────────────────────────
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

user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LLPROC, wintypes.HINSTANCE,
                                     wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                  wintypes.LPARAM]
# Every argtype here matters: without them ctypes guesses from the Python
# value and truncates 64-bit handles, which surfaces as
# "OverflowError: int too long to convert" on the HINSTANCE argument.
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
user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                   wintypes.LPVOID,
                                   ctypes.POINTER(wintypes.UINT), wintypes.UINT]
user32.GetRawInputDeviceInfoW.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                          wintypes.LPVOID,
                                          ctypes.POINTER(wintypes.UINT)]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0xA6: "BrowserBack", 0xA7: "BrowserForward", 0xA8: "BrowserRefresh",
    0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp",
    0xB0: "MediaNext", 0xB1: "MediaPrev", 0xB2: "MediaStop",
    0xB3: "MediaPlayPause", 0xB5: "LaunchMediaSelect",
    0xB6: "LaunchApp1", 0xB7: "LaunchApp2",
}
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"
for _c in list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)):
    VK_NAMES[_c] = chr(_c)


def vk_name(vk):
    return VK_NAMES.get(vk, f"0x{vk:02X}")


# ── Device names ────────────────────────────────────────────────────────────
def device_name(handle):
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, None, ctypes.byref(size))
    if not size.value:
        return "(unknown)"
    buf = ctypes.create_unicode_buffer(size.value)
    user32.GetRawInputDeviceInfoW(handle, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


def short_device(name):
    """Pull VID/PID out of a raw device path, which is what identifies it."""
    import re
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", name or "")
    return f"VID_{m.group(1).upper()}&PID_{m.group(2).upper()}" if m else (name or "?")


VENDORS = {
    "046D": "Logitech", "248A": "Telink (often a remote/air-mouse receiver)",
    "1038": "SteelSeries", "045E": "Microsoft", "413C": "Dell",
    "1A2C": "China Resource Semico", "05AC": "Apple", "5A4": "?",
}

# ── Collected evidence ──────────────────────────────────────────────────────
seq = 0
raw_events = []          # (seq, t, device_short, vk, makecode)
hook_events = []         # (seq, t, vk, scancode, injected)
per_device = defaultdict(lambda: defaultdict(int))   # device -> vk -> count
order_votes = {"raw_first": 0, "hook_first": 0}
_pending_raw = {}        # vk -> seq, so the hook can look for a preceding raw


def _next_seq():
    global seq
    seq += 1
    return seq


# ── Low-level keyboard hook (watch only — nothing is blocked) ───────────────
@_LLPROC
def _ll_proc(nCode, wParam, lParam):
    if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        s = _next_seq()
        injected = bool(kb.flags & 0x10)
        hook_events.append((s, time.perf_counter(), kb.vkCode, kb.scanCode, injected))
        # Did Raw Input already tell us about this key?
        prior = _pending_raw.pop(kb.vkCode, None)
        if prior is not None:
            order_votes["raw_first"] += 1
        else:
            order_votes["hook_first"] += 1
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


# ── Raw Input window ───────────────────────────────────────────────────────
@_WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        size = wintypes.UINT(0)
        user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, None,
                               ctypes.byref(size),
                               ctypes.sizeof(RAWINPUTHEADER))
        if size.value:
            buf = ctypes.create_string_buffer(size.value)
            user32.GetRawInputData(wintypes.HANDLE(lparam), RID_INPUT, buf,
                                   ctypes.byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
            ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
            if ri.header.dwType == RIM_TYPEKEYBOARD and \
                    ri.keyboard.Message in (WM_KEYDOWN, WM_SYSKEYDOWN):
                dev = short_device(device_name(ri.header.hDevice))
                vk = ri.keyboard.VKey
                s = _next_seq()
                raw_events.append((s, time.perf_counter(), dev, vk,
                                   ri.keyboard.MakeCode))
                per_device[dev][vk] += 1
                _pending_raw[vk] = s
                print(f"  {dev:<22} {vk_name(vk):<16} "
                      f"vk=0x{vk:02X} scan=0x{ri.keyboard.MakeCode:02X}")
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def make_window():
    cls = WNDCLASS()
    cls.lpfnWndProc = ctypes.cast(_wnd_proc, ctypes.c_void_p)
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.lpszClassName = "JHTRemoteProbe"
    if not user32.RegisterClassW(ctypes.byref(cls)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = user32.CreateWindowExW(0, "JHTRemoteProbe", "probe", 0, 0, 0, 0, 0,
                                  HWND_MESSAGE, None, cls.hInstance, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd


def register_raw_input(hwnd):
    rid = RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd)  # generic kbd
    if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1,
                                          ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())


def summarise():
    print("\n" + "─" * 70)
    if not per_device:
        print("No keypresses were seen. If the remote is a plain HID keyboard it")
        print("should have shown up — try again and press a few buttons.")
        return

    print("Keys seen, by device:\n")
    for dev, keys in sorted(per_device.items(), key=lambda kv: -sum(kv[1].values())):
        vid = dev.split("&")[0].replace("VID_", "")
        who = VENDORS.get(vid, "")
        print(f"  {dev}  {('— ' + who) if who else ''}")
        listed = ", ".join(f"{vk_name(v)}" for v in sorted(keys))
        print(f"      {sum(keys.values())} press(es): {listed}\n")

    total = order_votes["raw_first"] + order_votes["hook_first"]
    print("─" * 70)
    if total:
        raw_pct = 100 * order_votes["raw_first"] / total
        print(f"Raw Input arrived before the hook for {order_votes['raw_first']}"
              f"/{total} keys ({raw_pct:.0f}%).")
        if raw_pct > 90:
            print("→ The bridge can identify the device inside the hook and block")
            print("  the remote's keys precisely, leaving the keyboard untouched.")
        else:
            print("→ The hook usually runs first, so the bridge will route by")
            print("  'which device was used last'. Expect at most one keypress to")
            print("  slip through when you first pick the remote up.")

    if len(per_device) > 1:
        busiest = max(per_device.items(), key=lambda kv: sum(kv[1].values()))
        print("\nIf the remote is the device listed first above, put this in config.py:\n")
        print(f'REMOTE_DEVICE = "{busiest[0]}"')
    else:
        only = next(iter(per_device))
        print(f"\nOnly one device sent keys ({only}). Press the *other* one too so")
        print("the probe can tell them apart.")


def main():
    print(__doc__.split("How it works")[0].strip())
    print("\nWatching for keypresses — press remote buttons, then keyboard keys.")
    print("Press Ctrl+C when done.\n")

    hwnd = make_window()
    register_raw_input(hwnd)
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _ll_proc,
                                    kernel32.GetModuleHandleW(None), 0)
    if not hook:
        print("Could not install the keyboard hook:",
              ctypes.WinError(ctypes.get_last_error()))
        return

    msg = wintypes.MSG()
    deadline = None
    if "--seconds" in sys.argv:
        deadline = time.time() + float(sys.argv[sys.argv.index("--seconds") + 1])
    try:
        while True:
            if deadline and time.time() > deadline:
                break
            # PeekMessage keeps Ctrl+C responsive; GetMessage would block it.
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0x0001):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(hook)
        summarise()


if __name__ == "__main__":
    main()
