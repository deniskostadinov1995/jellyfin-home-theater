"""
Jellyfin Home Theater — USB Remote Launcher
===========================================
Runs quietly in the background and watches for ONE button on your USB remote.
When you press it, it makes sure both of these are running (and starts whichever
isn't):

    1) Jellyfin Home Theater  (this project's app.py — the TV UI + watcher)
    2) SignalRGB              (your ambient-lighting app)

Pressing the button again when both are already up does nothing, so it's safe
to mash it. The button keeps working normally inside the app — this only adds
the launch behaviour on top, it doesn't swallow the keypress.

No third-party packages required — it uses the built-in Windows API via ctypes
(the same approach as monitor_watcher.py), so there's nothing to pip install.

────────────────────────────────────────────────────────────────────────────
ONE-TIME SETUP
────────────────────────────────────────────────────────────────────────────
1) Find out which key your remote button sends:

       python remote_launcher.py --detect

   Press the button once. It prints something like:
       HOTKEY_VK = 0xB3      # MediaPlayPause     (modifiers: none)
   Copy the HOTKEY_VK value into the config below. If it also lists modifiers
   (e.g. "ctrl, alt"), set HOTKEY_MODS to match.

2) Test it without rebooting:

       python remote_launcher.py

   Press your button — both apps should come up. Ctrl+C to stop watching.

3) Make it start automatically every time you log in:

       python remote_launcher.py --install

   (Undo with  --uninstall .)  After --install it runs invisibly at logon,
   so the remote button "just works" from a cold boot.

────────────────────────────────────────────────────────────────────────────
Notes
────────────────────────────────────────────────────────────────────────────
• Pick a button your remote has that you don't use for anything else. If
  --detect reports a normal letter/number with no modifiers, choose a
  different button (or one with modifiers), or it'll fire while you type.
• Activity is logged to  remote_launcher.log  next to this script.
• If SignalRGB isn't found automatically, set SIGNALRGB_EXE below.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import os
import sys
import glob
import time
import socket
import ctypes
import threading
import subprocess
from ctypes import wintypes
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG  — edit these
# ─────────────────────────────────────────────────────────────────────────────

# The remote button's virtual-key code, from  --detect .  Examples:
#   0xB3 = Play/Pause media   0xB0 = Next track   0x7C = F13   0xB6 = LaunchApp1
HOTKEY_VK = 0x82

# Only needed if your button sends a COMBO. Most remote buttons don't — leave
# empty. Otherwise list what --detect reported, e.g. ["ctrl", "alt"].
HOTKEY_MODS = []

# Port the Jellyfin Home Theater server listens on (matches PORT in config.py).
APP_PORT = 5050

# Folder containing app.py.  Defaults to this script's own folder.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Leave blank to auto-detect SignalRGB.  Only set this if auto-detect fails —
# point it at SignalRgb.exe or SignalRgbLauncher.exe.
SIGNALRGB_EXE = ""

# Ignore repeat presses within this many seconds (debounce).
COOLDOWN_SECS = 3.0

# ─────────────────────────────────────────────────────────────────────────────
#  Logging / helpers
# ─────────────────────────────────────────────────────────────────────────────

LOG_PATH = os.path.join(PROJECT_DIR, "remote_launcher.log")
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW — keep helper processes silent


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def python_exe():
    """Prefer pythonw.exe (no console window) next to the current interpreter."""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return pyw if os.path.exists(pyw) else exe


# ── Jellyfin Home Theater ────────────────────────────────────────────────────
def app_running():
    """True if something is already listening on the app's port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", APP_PORT)) == 0


def start_app():
    app = os.path.join(PROJECT_DIR, "app.py")
    if not os.path.exists(app):
        log(f"ERROR: app.py not found in {PROJECT_DIR} — set PROJECT_DIR.")
        return
    subprocess.Popen(
        [python_exe(), "app.py"],
        cwd=PROJECT_DIR,
        creationflags=_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    log("Started Jellyfin Home Theater")


# ── SignalRGB ────────────────────────────────────────────────────────────────
def signalrgb_running():
    """True if the SignalRGB app itself is running (not just a helper/service)."""
    try:
        out = subprocess.run(
            ["tasklist"], capture_output=True, text=True,
            creationflags=_NO_WINDOW,
        ).stdout.lower()
    except Exception:
        return False
    # Match the main app image "signalrgb.exe" exactly — deliberately NOT
    # "signalrgblauncher.exe" or "signalrgbservice.exe", so the button
    # relaunches the app when you've actually closed it.
    return "signalrgb.exe" in out


def find_signalrgb():
    """Locate the best thing to launch SignalRGB with — prefers update-proof
    launchers over the version-stamped app exe (which changes on every update)."""
    if SIGNALRGB_EXE and os.path.exists(SIGNALRGB_EXE):
        return SIGNALRGB_EXE

    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("PROGRAMFILES", "")
    pfx = os.environ.get("PROGRAMFILES(X86)", "")

    # 1) The vendor's version-independent launcher stub (best — survives updates)
    launcher = os.path.join(la, "VortxEngine", "SignalRgbLauncher.exe")
    if os.path.exists(launcher):
        return launcher

    # 2) A Start-menu shortcut, which the installer maintains across updates.
    for menu in (
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ):
        for hit in glob.glob(os.path.join(menu, "**", "SignalRgb*.lnk"),
                             recursive=True):
            return hit

    # 3) Known fixed exe locations.
    for c in (
        os.path.join(la, "Programs", "WhirlwindFX", "SignalRGB", "SignalRgb.exe"),
        os.path.join(la, "WhirlwindFX", "SignalRgb", "SignalRgb.exe"),
        os.path.join(pf, "WhirlwindFX", "SignalRGB", "SignalRgb.exe"),
    ):
        if os.path.exists(c):
            return c

    # 4) Last resort: the version-stamped app exe (shallow globs only).
    for base in (la, os.path.join(la, "Programs"), pf, pfx):
        if not base:
            continue
        for depth in ("*", "*/*", "*/*/*"):
            for hit in glob.glob(os.path.join(base, depth, "SignalRgb.exe")):
                return hit
    return None


def start_signalrgb():
    path = find_signalrgb()
    if not path:
        log("Could not find SignalRGB — set SIGNALRGB_EXE in remote_launcher.py")
        return
    try:
        os.startfile(path)  # works for both .exe and .lnk
        log(f"Started SignalRGB  ({os.path.basename(path)})")
    except OSError as e:
        log(f"Failed to launch SignalRGB: {e}")


# ── The action bound to the button ───────────────────────────────────────────
def ensure_running():
    if app_running():
        log("Jellyfin Home Theater already running — skipped")
    else:
        start_app()

    if signalrgb_running():
        log("SignalRGB already running — skipped")
    else:
        start_signalrgb()


# ─────────────────────────────────────────────────────────────────────────────
#  Global keyboard hook (pure Win32 via ctypes — no dependencies)
# ─────────────────────────────────────────────────────────────────────────────

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_QUIT = 0x0012
PM_REMOVE = 0x0001
LRESULT = ctypes.c_ssize_t

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


# WINFUNCTYPE for the low-level keyboard proc
_LLPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# Set arg/return types so 64-bit handles aren't truncated (the classic pitfall).
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LLPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

# Virtual-key codes that are pure modifiers (ignored when detecting the button).
_MOD_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5}

VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space",
    0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0xA6: "BrowserBack", 0xA7: "BrowserForward", 0xA8: "BrowserRefresh",
    0xA9: "BrowserStop", 0xAA: "BrowserSearch", 0xAB: "BrowserFavorites",
    0xAC: "BrowserHome",
    0xAD: "VolumeMute", 0xAE: "VolumeDown", 0xAF: "VolumeUp",
    0xB0: "MediaNext", 0xB1: "MediaPrev", 0xB2: "MediaStop", 0xB3: "MediaPlayPause",
    0xB4: "LaunchMail", 0xB5: "LaunchMediaSelect", 0xB6: "LaunchApp1", 0xB7: "LaunchApp2",
}
for _i in range(24):
    VK_NAMES[0x70 + _i] = f"F{_i + 1}"          # F1..F24
for _c in range(0x30, 0x3A):
    VK_NAMES[_c] = chr(_c)                        # 0-9
for _c in range(0x41, 0x5B):
    VK_NAMES[_c] = chr(_c)                        # A-Z


def _mods_down():
    def down(vk):
        return (user32.GetAsyncKeyState(vk) & 0x8000) != 0
    mods = []
    if down(0x11):
        mods.append("ctrl")
    if down(0x12):
        mods.append("alt")
    if down(0x10):
        mods.append("shift")
    if down(0x5B) or down(0x5C):
        mods.append("win")
    return mods


def _run_hook(on_keydown):
    """Install a low-level keyboard hook and pump messages until WM_QUIT.
    on_keydown(vk, scan, mods) is called for every key-down event."""
    def _cb(nCode, wParam, lParam):
        if nCode == 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            try:
                on_keydown(kb.vkCode, kb.scanCode, _mods_down())
            except Exception as e:
                log(f"hook callback error: {e}")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    proc = _LLPROC(_cb)                 # keep referenced for the hook's lifetime
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc,
                                    kernel32.GetModuleHandleW(None), 0)
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())

    msg = wintypes.MSG()
    try:
        while True:
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.008)          # idle a hair so we don't spin a core
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnhookWindowsHookEx(hook)


# ── Modes ────────────────────────────────────────────────────────────────────
def detect_mode():
    print("Press your remote button once…  (Ctrl+C to cancel)\n")
    found = {}

    def on_key(vk, scan, mods):
        if vk in _MOD_VKS:
            return                          # wait for the real key, not a modifier
        found["vk"], found["scan"], found["mods"] = vk, scan, mods
        user32.PostQuitMessage(0)

    _run_hook(on_key)
    if not found:
        print("No key captured.")
        return
    vk = found["vk"]
    name = VK_NAMES.get(vk, "?")
    mods = found["mods"]
    print(f'HOTKEY_VK = 0x{vk:02X}      # {name}     '
          f'(modifiers: {", ".join(mods) if mods else "none"})')
    print(f"   scan code: 0x{found['scan']:02X}")
    print("\nPut that HOTKEY_VK value near the top of remote_launcher.py.")
    if mods:
        print(f'Also set  HOTKEY_MODS = {mods}')
    elif 0x30 <= vk <= 0x5A:
        print("⚠ That's a normal letter/number key — it will fire whenever you "
              "type it. Pick a dedicated remote button, or add modifiers.")


def listen_mode():
    # Single-instance guard so logon-start + a manual run don't both listen.
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", 57333))
    except OSError:
        log("Another launcher instance is already running — exiting.")
        return

    last = [0.0]

    def on_key(vk, scan, mods):
        if vk != HOTKEY_VK:
            return
        if HOTKEY_MODS and not all(m in mods for m in HOTKEY_MODS):
            return
        now = time.time()
        if now - last[0] < COOLDOWN_SECS:
            return
        last[0] = now
        log(f"Button pressed (vk=0x{vk:02X}) — checking apps…")
        # Do the launching off the hook thread so the callback returns instantly
        # (a slow low-level hook callback gets silently removed by Windows).
        threading.Thread(target=ensure_running, daemon=True).start()

    name = VK_NAMES.get(HOTKEY_VK, f"0x{HOTKEY_VK:02X}")
    mod_txt = ("+".join(HOTKEY_MODS) + "+") if HOTKEY_MODS else ""
    log(f"Watching for remote button: {mod_txt}{name}  (Ctrl+C to stop)")
    _run_hook(on_key)
    log("Stopped.")


# ── Auto-start at logon (Startup-folder shortcut) ────────────────────────────
def _startup_dir():
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _startup_lnk_path():
    return os.path.join(_startup_dir(), "JellyfinRemoteLauncher.lnk")


# Pre-Jellyfin name of the same shortcut.  Installs and uninstalls clear it out
# too, so an upgraded setup doesn't end up launching twice at logon.
def _legacy_lnk_path():
    return os.path.join(_startup_dir(), "PlexRemoteLauncher.lnk")


def _remove_legacy_lnk():
    legacy = _legacy_lnk_path()
    if os.path.exists(legacy):
        try:
            os.remove(legacy)
            log("Removed the old PlexRemoteLauncher.lnk from Startup.")
        except OSError as e:
            log(f"Could not remove {legacy}: {e}")


def install_startup():
    lnk = _startup_lnk_path()
    target = python_exe()
    script = os.path.abspath(__file__)
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
        "$s.TargetPath='{target}';"
        "$s.Arguments='\"{script}\"';"
        "$s.WorkingDirectory='{wd}';"
        "$s.WindowStyle=7;"
        "$s.Save()"
    ).format(lnk=lnk, target=target, script=script, wd=PROJECT_DIR)
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    _remove_legacy_lnk()
    log(f"Installed to Startup: {lnk}")
    log("It will start automatically next time you log in.")


def uninstall_startup():
    lnk = _startup_lnk_path()
    removed = False
    if os.path.exists(lnk):
        os.remove(lnk)
        log("Removed from Startup.")
        removed = True
    before = os.path.exists(_legacy_lnk_path())
    _remove_legacy_lnk()
    if not removed and not before:
        log("Nothing to remove — no Startup shortcut found.")


def main():
    args = set(sys.argv[1:])
    if "--detect" in args:
        detect_mode()
    elif "--install" in args:
        install_startup()
    elif "--uninstall" in args:
        uninstall_startup()
    elif "--once" in args:
        ensure_running()          # run the launch action once, no key listening
    else:
        listen_mode()


if __name__ == "__main__":
    main()
