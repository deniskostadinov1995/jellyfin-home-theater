"""
Monitor Watcher - Jellyfin Home Theater
-------------------------------------
Polls a named Windows display and decides whether the TV is *actively*
showing our HDMI output (not merely plugged in / powered on).

This is the hard part of the whole project: HDMI is mostly a one-way pipe,
so the PC cannot simply "ask the TV what it is showing" in a way every TV
answers reliably.  We therefore use a small *layered confidence model* and
take the strongest signal each TV is willing to give us.  In order of
preference:

  1) DDC/CI input-source register  (VCP 0x60)  -- the ideal signal.
     Asks the TV "which input are you displaying right now?".  When the TV
     is on our HDMI port it returns that port's code (0x11=HDMI1 ...); when
     it is on a different input it returns a different code.  This tells us
     *exactly* what we want.  Most TVs (LG, Samsung, Sony, Philips, TCL,
     Hisense) support reading it -- but DDC/CI runs over a slow, noisy I2C
     bus, so a *single* read fails very often.  The fix is to (a) open the
     physical-monitor handle ONCE and reuse it, and (b) retry the read a
     few times before believing a "no answer".  That alone turns the old
     "TV did not respond" spam into a reliable reading on the same hardware.

  2) DDC/CI power register  (VCP 0xD6)  -- the fallback signal.
     Some cheaper TVs answer power state but not input source.  0xD6 tells
     us On (0x01) vs Standby/Off (>=0x02).  It cannot tell which input, but
     it at least stops us launching onto a TV that is in standby.  Used only
     when 0x60 is unsupported and FALLBACK_TO_POWER_STATE is on.

  3) Display enumeration  (EnumDisplayMonitors)  -- the last resort.
     Confirms Windows still sees the display at all.  A TV that is OFF or
     unplugged usually drops the HDMI hot-plug signal and disappears here,
     so this catches the off/unplugged case for every TV.  Used as the base
     gate (no display -> definitely inactive) and as the only signal when
     the TV answers no DDC/CI at all.

Capability is *learned*: the detector reuses whatever worked and stops
hammering (and stops warning about) registers a given TV never answers.

When the TV switches AWAY from our HDMI input -- or is turned off -- the
program kills the Chrome kiosk window so the user does not return to a
stale UI when they come back.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

import ctypes
import ctypes.wintypes as wt
import logging
import os
import subprocess
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)


# ── Win32 monitor enumeration (pure ctypes, no pywin32) ───────────────────

class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

class _MONITORINFOEX(ctypes.Structure):
    MONITORINFOF_PRIMARY = 0x00000001
    _fields_ = [
        ("cbSize",    ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork",    _RECT),
        ("dwFlags",   ctypes.c_ulong),
        ("szDevice",  ctypes.c_wchar * 32),
    ]


def _enum_display_monitors_raw():
    """Yield (hMonitor, info_dict) tuples for every connected display."""
    out = []

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_long), ctypes.c_double,
    )

    user32 = ctypes.windll.user32

    def _cb(hMon, _hdc, _lprc, _data):
        info = _MONITORINFOEX()
        info.cbSize = ctypes.sizeof(_MONITORINFOEX)
        if user32.GetMonitorInfoW(hMon, ctypes.byref(info)):
            r = info.rcMonitor
            out.append((hMon, {
                "name":       info.szDevice,
                "x":          r.left,
                "y":          r.top,
                "width":      r.right  - r.left,
                "height":     r.bottom - r.top,
                "is_primary": bool(info.dwFlags & _MONITORINFOEX.MONITORINFOF_PRIMARY),
            }))
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    return out


def _enum_display_monitors():
    """Backwards-compatible: return list of info dicts only."""
    try:
        from screeninfo import get_monitors
        return [
            {
                "name":       m.name,
                "x":          m.x,
                "y":          m.y,
                "width":      m.width,
                "height":     m.height,
                "is_primary": m.is_primary,
            }
            for m in get_monitors()
        ]
    except Exception as exc:
        log.debug("screeninfo not available (%s); using ctypes.", exc)
    return [info for _, info in _enum_display_monitors_raw()]


def _find_target(target_name: str) -> Optional[dict]:
    name_lower = target_name.lower()
    for m in _enum_display_monitors():
        if m["name"].lower() == name_lower:
            return m
    return None


def _find_target_hmon(target_name: str) -> Tuple[Optional[int], Optional[dict]]:
    """Return (hMonitor, info) for target display, or (None, None)."""
    name_lower = target_name.lower()
    for hMon, info in _enum_display_monitors_raw():
        if info["name"].lower() == name_lower:
            return hMon, info
    return None, None


# ── DDC/CI: read VCP registers off the TV ────────────────────────────────
# VCP feature codes (VESA MCCS):
#   0x60 = "Input Source Select" — which input the TV is displaying now
#   0xD6 = "Power Mode"          — 0x01 On · 0x02 Standby · 0x04 Off
# Common 0x60 values (vary per manufacturer):
#   0x01 VGA1   0x03 DVI1   0x04 DVI2
#   0x11 HDMI1  0x12 HDMI2  0x13 HDMI3  0x14 HDMI4
#   0x0F DisplayPort1   0x10 DisplayPort2
# Many TVs use the 0x11..0x14 range for their HDMI inputs.

VCP_INPUT_SOURCE = 0x60
VCP_POWER_MODE   = 0xD6
POWER_ON         = 0x01


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor",               wt.HANDLE),
        ("szPhysicalMonitorDescription",   wt.WCHAR * 128),
    ]


class InputSourceDetector:
    """
    Robust DDC/CI reader with a persistent physical-monitor handle, retries,
    and learned per-TV capability.

    Why a class and not the old one-shot helper:
      * The physical-monitor handle is opened ONCE and reused.  The old code
        opened + destroyed it on every poll, which pounds the I2C bus and is
        a big source of "no response".
      * Each read is retried a few times.  DDC/CI is noisy; a single miss is
        normal and means nothing.  We only believe a "no answer" after the
        retries are exhausted.
      * We remember which registers a given TV actually answers, so we stop
        probing (and stop warning about) ones it never will.

    All ctypes calls happen inside a worker thread guarded by a timeout, so a
    hung TV can never wedge the watcher loop.
    """

    def __init__(self,
                 expected_input_vcp: Optional[int] = None,
                 tries: int = 4,
                 retry_ms: int = 60,
                 query_timeout_s: float = 2.5):
        self.expected_input_vcp = expected_input_vcp
        self.tries           = max(1, tries)
        self.retry_ms        = max(0, retry_ms)
        self.query_timeout_s = query_timeout_s

        # Learned capability: None = unknown, True/False once decided.
        self.input_supported: Optional[bool] = None
        self.power_supported: Optional[bool] = None

        self._dxva2 = None
        self._fns_ready = False

        # Cached handle, tied to the HMONITOR it was opened from.
        self._handle: Optional[int] = None
        self._handle_hmon: Optional[int] = None
        self._arr = None          # keep the _PHYSICAL_MONITOR array alive
        self._n = 0

    # ── lazy symbol resolution ───────────────────────────────────────────
    def _ensure_fns(self) -> bool:
        if self._fns_ready:
            return self._dxva2 is not None
        self._fns_ready = True
        try:
            self._dxva2 = ctypes.WinDLL("dxva2.dll")
        except OSError:
            log.warning("dxva2.dll not available — DDC/CI disabled.")
            self._dxva2 = None
            return False

        d = self._dxva2
        d.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [
            wt.HMONITOR, ctypes.POINTER(wt.DWORD)]
        d.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wt.BOOL

        d.GetPhysicalMonitorsFromHMONITOR.argtypes = [
            wt.HMONITOR, wt.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
        d.GetPhysicalMonitorsFromHMONITOR.restype = wt.BOOL

        d.DestroyPhysicalMonitors.argtypes = [
            wt.DWORD, ctypes.POINTER(_PHYSICAL_MONITOR)]
        d.DestroyPhysicalMonitors.restype = wt.BOOL

        d.GetVCPFeatureAndVCPFeatureReply.argtypes = [
            wt.HANDLE,                  # hMonitor
            wt.BYTE,                    # bVCPCode
            ctypes.POINTER(wt.DWORD),   # pvct (type)
            ctypes.POINTER(wt.DWORD),   # pdwCurrentValue
            ctypes.POINTER(wt.DWORD),   # pdwMaximumValue
        ]
        d.GetVCPFeatureAndVCPFeatureReply.restype = wt.BOOL
        return True

    # ── handle lifecycle ─────────────────────────────────────────────────
    def _open_handle(self, hMonitor: int) -> bool:
        """Open (and cache) the physical-monitor handle for hMonitor."""
        if not self._ensure_fns():
            return False
        d = self._dxva2

        n = wt.DWORD(0)
        if not d.GetNumberOfPhysicalMonitorsFromHMONITOR(hMonitor, ctypes.byref(n)):
            return False
        if n.value == 0:
            return False

        arr = (_PHYSICAL_MONITOR * n.value)()
        if not d.GetPhysicalMonitorsFromHMONITOR(hMonitor, n.value, arr):
            return False

        self._arr = arr
        self._n = n.value
        self._handle = arr[0].hPhysicalMonitor
        self._handle_hmon = hMonitor
        return True

    def release(self):
        """Destroy the cached handle (call on display change / shutdown)."""
        if self._dxva2 and self._arr is not None and self._n:
            try:
                self._dxva2.DestroyPhysicalMonitors(self._n, self._arr)
            except Exception:
                pass
        self._handle = None
        self._handle_hmon = None
        self._arr = None
        self._n = 0

    def _handle_for(self, hMonitor: int) -> Optional[int]:
        """Return a valid cached handle for hMonitor, reopening if needed."""
        if not hMonitor:
            return None
        if self._handle is not None and self._handle_hmon == hMonitor:
            return self._handle
        # Topology changed (or first use): drop the stale handle and reopen.
        self.release()
        if self._open_handle(hMonitor):
            return self._handle
        return None

    # ── the actual read (worker-thread + timeout + retries) ──────────────
    def _read_vcp(self, hMonitor: int, code: int) -> Optional[int]:
        result = {"value": None}

        def _do():
            handle = self._handle_for(hMonitor)
            if handle is None:
                return
            d = self._dxva2
            for attempt in range(self.tries):
                current = wt.DWORD(0)
                maxv    = wt.DWORD(0)
                vct     = wt.DWORD(0)
                ok = d.GetVCPFeatureAndVCPFeatureReply(
                    handle, wt.BYTE(code),
                    ctypes.byref(vct), ctypes.byref(current), ctypes.byref(maxv),
                )
                if ok:
                    result["value"] = current.value
                    return
                if attempt + 1 < self.tries and self.retry_ms:
                    time.sleep(self.retry_ms / 1000.0)

        t = threading.Thread(target=_do, daemon=True, name="DDCIRead")
        t.start()
        t.join(self.query_timeout_s)
        return result["value"]

    # ── public reads, with capability learning ───────────────────────────
    def read_input_source(self, hMonitor: int) -> Optional[int]:
        v = self._read_vcp(hMonitor, VCP_INPUT_SOURCE)
        if v is not None:
            if self.input_supported is not True:
                log.info("DDC/CI input-source (VCP 0x60) is supported on this TV.")
            self.input_supported = True
        return v

    def read_power(self, hMonitor: int) -> Optional[int]:
        v = self._read_vcp(hMonitor, VCP_POWER_MODE)
        if v is not None:
            if self.power_supported is not True:
                log.info("DDC/CI power-mode (VCP 0xD6) is supported on this TV.")
            self.power_supported = True
        return v


# ── Win32 window mover ───────────────────────────────────────────────────

def _force_window_to_monitor(pid: int, monitor: dict, timeout: float = 10.0):
    """Move/maximize Chrome's window on the right monitor (kiosk fix)."""
    user32 = ctypes.windll.user32

    SW_RESTORE   = 9
    SW_MAXIMIZE  = 3
    HWND_TOP     = 0
    SWP_SHOWWINDOW = 0x0040

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_ulong, ctypes.c_ulong
    )

    deadline = time.monotonic() + timeout

    def _find_hwnd():
        result = ctypes.c_ulong(0)

        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            win_pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value == pid:
                result.value = hwnd
                return False
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return result.value or None

    hwnd = None
    while time.monotonic() < deadline:
        hwnd = _find_hwnd()
        if hwnd:
            break
        time.sleep(0.4)

    if not hwnd:
        log.warning("Could not find Chrome window for pid %d - skipping reposition.", pid)
        return

    x, y, w, h = monitor["x"], monitor["y"], monitor["width"], monitor["height"]
    log.info(
        "Repositioning Chrome (hwnd=%d pid=%d) -> %s (%d,%d) %dx%d",
        hwnd, pid, monitor["name"], x, y, w, h,
    )

    user32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(0.15)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, SWP_SHOWWINDOW)
    time.sleep(0.15)
    user32.ShowWindow(hwnd, SW_MAXIMIZE)
    user32.SetForegroundWindow(hwnd)


# ── Chrome launcher ──────────────────────────────────────────────────────

def _launch_chrome(monitor: dict, port: int, chrome_path: str) -> Optional[subprocess.Popen]:
    url         = f"http://localhost:{port}/tv"
    profile_dir = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "JellyfinKioskProfile")

    cmd = [
        chrome_path,
        f"--user-data-dir={profile_dir}",
        f"--window-position={monitor['x']},{monitor['y']}",
        f"--window-size={monitor['width']},{monitor['height']}",
        "--kiosk",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--no-first-run",
        "--autoplay-policy=no-user-gesture-required",

        # ── Smooth-playback flags ────────────────────────────────────────
        # Stop Chrome throttling the page when the kiosk window is not the
        # foreground window (the Flask console, the reposition step, or the
        # OS can briefly steal focus).  Throttling a backgrounded/occluded
        # tab starves the video pipeline and is the #1 cause of micro-stutter
        # in a kiosk like this.
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        # Force GPU video decode/compositing even on drivers Chrome would
        # otherwise distrust — keeps frame pacing smooth instead of falling
        # back to slower software paths.
        "--ignore-gpu-blocklist",
        "--enable-gpu-rasterization",
        "--enable-zero-copy",
        # NOTE: Chrome only honours the LAST --disable-features flag, so every
        # disabled feature must live in this one comma-separated list.
        # CalculateNativeWinOcclusion is what marks the kiosk window as
        # "occluded" on Windows; disabling it stops the throttling above from
        # ever kicking in.
        "--disable-features=TranslateUI,CalculateNativeWinOcclusion",
        url,
    ]

    log.info(
        "Launching Chrome on %s  pos=(%d,%d)  size=%dx%d  profile=%s",
        monitor["name"], monitor["x"], monitor["y"],
        monitor["width"], monitor["height"], profile_dir,
    )

    try:
        proc = subprocess.Popen(cmd)
    except FileNotFoundError:
        log.error("Chrome not found at: %s  -- update CHROME_PATH in config.py.", chrome_path)
        return None
    except Exception as exc:
        log.error("Failed to launch Chrome: %s", exc)
        return None

    threading.Thread(
        target=_force_window_to_monitor,
        args=(proc.pid, monitor),
        daemon=True,
        name="ChromeReposition",
    ).start()

    return proc


def _kill_chrome(proc: Optional[subprocess.Popen]):
    if proc is None:
        return
    if proc.poll() is None:
        log.info("Terminating Chrome (pid %d) - display disconnected.", proc.pid)
        try:
            proc.terminate()
        except Exception:
            pass


# ── Public API ───────────────────────────────────────────────────────────

def start_monitor_watcher(
    target_name: str,
    chrome_path: str,
    port: int,
    poll_interval: int = 5,
    require_active_input: bool = False,
    expected_input_vcp: Optional[int] = None,
    fallback_to_power_state: bool = True,
) -> threading.Thread:
    """
    Spawn a daemon thread that watches for `target_name` and launches /
    tears down the kiosk based on what the TV is actually doing.

    Args:
        target_name:          Display name as Windows reports it, e.g. r"\\\\.\\DISPLAY3".
        chrome_path:          Full path to chrome.exe.
        port:                 Flask port.
        poll_interval:        Seconds between polls (default 5).
        require_active_input: When True, use the DDC/CI layered model (input
                              source, then power state) to require the TV to be
                              showing OUR input before launching.  When False,
                              fall back to plain "is the display enumerated?".
        expected_input_vcp:   The VCP 0x60 value that means "our HDMI input", or
                              None to auto-learn it on the first successful read
                              (assumes you are on the correct input at startup).
        fallback_to_power_state:
                              When the TV answers power (0xD6) but not input
                              (0x60), require power == On.  This can't tell which
                              input the TV is on, so it may launch onto the wrong
                              input — but it still avoids launching into standby.

    Returns:
        The running daemon Thread.
    """

    # A register is only declared "unsupported" after this many consecutive
    # poll cycles with no answer (each cycle is itself several retries), so a TV
    # that is simply mid-handshake on the first poll isn't written off early.
    UNSUPPORTED_AFTER = 3

    # Many TVs only service DDC/CI on the *active* input: once the user
    # switches to another HDMI port, reads that used to work go silent.
    # So on a TV that is KNOWN to answer 0x60, sustained silence means
    # "switched away or off", not "unsupported".  After this many silent
    # cycles we treat the TV as inactive instead of falling through to the
    # (much weaker) presence check.
    KNOWN_SILENT_AFTER = 3

    def _watch():
        nonlocal expected_input_vcp
        was_active  = False
        chrome_proc: Optional[subprocess.Popen] = None
        detector = InputSourceDetector(expected_input_vcp=expected_input_vcp)
        warned_no_ddc = False   # only nag about a fully-unresponsive TV once
        misses = {"input": 0, "power": 0, "input_known": 0}

        log.info(
            "Monitor watcher started — polling every %ds for: %s "
            "(active-input check: %s)",
            poll_interval, target_name,
            "ON" if require_active_input else "off",
        )

        def _classify(hMon, connected):
            """Return (is_active, reason) using the strongest available signal."""
            nonlocal expected_input_vcp, warned_no_ddc

            # Base gate: a display Windows can't even see is never active
            # (TV off / unplugged / dropped the HDMI hot-plug line).
            if not connected:
                return False, "display not connected"

            # No active-input check requested → enumeration is enough.
            if not require_active_input:
                return True, "display connected"

            # Layer 1 — input source (the signal we actually want).
            if detector.input_supported is not False:
                vcp = detector.read_input_source(hMon)
                if vcp is not None:
                    misses["input"] = 0
                    misses["input_known"] = 0
                    if expected_input_vcp is None:
                        expected_input_vcp = vcp
                        detector.expected_input_vcp = vcp
                        log.info(
                            "Learned TV input VCP code = 0x%02X. Pin it in "
                            "config.py with  EXPECTED_INPUT_VCP = 0x%02X  to skip "
                            "auto-learning next start.", vcp, vcp,
                        )
                        return True, "input source learned (0x%02X)" % vcp
                    if vcp == expected_input_vcp:
                        return True, "input source 0x%02X == expected" % vcp
                    return False, ("input source 0x%02X != expected 0x%02X "
                                   "(TV on another input)" % (vcp, expected_input_vcp))
                # 0x60 didn't answer this cycle.
                if detector.input_supported is True:
                    # This TV DOES answer 0x60 when it's on our input, so
                    # sustained silence is itself a signal: the TV has almost
                    # certainly been switched to another input (or gone dark).
                    misses["input_known"] += 1
                    if misses["input_known"] >= KNOWN_SILENT_AFTER:
                        if misses["input_known"] == KNOWN_SILENT_AFTER:
                            # Drop the cached handle once so the eventual
                            # recovery read starts from a fresh connection.
                            detector.release()
                        return False, ("DDC/CI silent on a TV that answers "
                                       "0x60 — likely on another input / off")
                    return False, "waiting for DDC/CI input-source reply"
                # Support still unknown: only conclude "unsupported" once it
                # has stayed silent for several poll cycles.
                if detector.input_supported is None:
                    misses["input"] += 1
                    if misses["input"] >= UNSUPPORTED_AFTER:
                        detector.input_supported = False
                        log.info(
                            "TV does not answer DDC/CI input-source (VCP 0x60); "
                            "falling back to %s.",
                            "power-state detection" if fallback_to_power_state
                            else "display-presence only",
                        )
                    else:
                        # Still hoping for an answer — don't launch yet.
                        return False, "waiting for DDC/CI input-source reply"

            # Layer 2 — power state (can't tell which input, only on vs standby).
            if fallback_to_power_state and detector.power_supported is not False:
                pwr = detector.read_power(hMon)
                if pwr is not None:
                    misses["power"] = 0
                    if pwr == POWER_ON:
                        return True, "power=On (input unknown)"
                    return False, "power=0x%02X (standby/off)" % pwr
                if detector.power_supported is None:
                    misses["power"] += 1
                    if misses["power"] >= UNSUPPORTED_AFTER:
                        detector.power_supported = False
                    else:
                        return False, "waiting for DDC/CI power reply"

            # Layer 3 — nothing answered.  Best we can do is "is it enumerated?".
            if not warned_no_ddc:
                warned_no_ddc = True
                log.warning(
                    "TV answers no DDC/CI registers — cannot tell which input "
                    "it is on.  Using display-presence only (set "
                    "REQUIRE_ACTIVE_INPUT=False in config.py to silence this).")
            return True, "display connected (DDC/CI unavailable)"

        while True:
            try:
                hMon, monitor = _find_target_hmon(target_name)
                connected = monitor is not None
                if not connected:
                    detector.release()   # drop the stale handle for next time

                is_active, reason = _classify(hMon, connected)

                if is_active and not was_active:
                    log.info("Display '%s' active (%s) — starting the TV UI…",
                             target_name, reason)
                    time.sleep(2)  # let Windows finish init
                    _, monitor = _find_target_hmon(target_name)
                    if monitor:
                        _kill_chrome(chrome_proc)
                        chrome_proc = _launch_chrome(monitor, port, chrome_path)

                elif not is_active and was_active:
                    log.info("Stopping the TV UI (%s).", reason)
                    _kill_chrome(chrome_proc)
                    chrome_proc = None

                was_active = is_active

            except Exception as exc:
                log.error("Watcher loop error: %s", exc)

            time.sleep(poll_interval)

    thread = threading.Thread(target=_watch, daemon=True, name="MonitorWatcher")
    thread.start()
    return thread


# ── Standalone test mode ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print()
    print("=" * 54)
    print("   monitor_watcher.py -- standalone test")
    print("=" * 54)

    monitors_raw = _enum_display_monitors_raw()
    if not monitors_raw:
        print("\n  No displays found. Make sure you are on Windows.\n")
        sys.exit(1)

    print(f"\n  Currently connected displays ({len(monitors_raw)} found):\n")
    probe = InputSourceDetector()
    for hMon, m in monitors_raw:
        tag = "  <- PRIMARY" if m["is_primary"] else ""
        print(f"    Name     : {m['name']}{tag}")
        print(f"    Position : ({m['x']}, {m['y']})")
        print(f"    Size     : {m['width']} x {m['height']} px")
        # Probe DDC/CI (retried + persistent handle) for input source + power
        probe.release()
        vcp = probe.read_input_source(hMon)
        pwr = probe.read_power(hMon)
        if vcp is None:
            print(f"    DDC 0x60 : (no response — TV may not support it)")
        else:
            print(f"    DDC 0x60 : 0x{vcp:02X}  <- current input source")
        if pwr is None:
            print(f"    DDC 0xD6 : (no response)")
        else:
            state = "On" if pwr == POWER_ON else "Standby/Off"
            print(f"    DDC 0xD6 : 0x{pwr:02X}  <- power: {state}")
        print()
    probe.release()

    try:
        from config import (
            MONITOR_NAME, CHROME_PATH, PORT, MONITOR_POLL_INTERVAL,
        )
    except ImportError:
        print("  ERROR: config.py not found. Run from your project folder.")
        sys.exit(1)
    try:
        from config import REQUIRE_ACTIVE_INPUT
    except ImportError:
        REQUIRE_ACTIVE_INPUT = False
    try:
        from config import EXPECTED_INPUT_VCP
    except ImportError:
        EXPECTED_INPUT_VCP = None
    try:
        from config import FALLBACK_TO_POWER_STATE
    except ImportError:
        FALLBACK_TO_POWER_STATE = True

    if not MONITOR_NAME:
        print("  MONITOR_NAME is None in config.py -- watcher is disabled.")
        sys.exit(0)

    hMon, match = _find_target_hmon(MONITOR_NAME)
    if match:
        print(f"  Target '{MONITOR_NAME}' is connected.")
        if REQUIRE_ACTIVE_INPUT:
            probe.release()
            vcp = probe.read_input_source(hMon)
            if vcp is None:
                print("  DDC/CI: TV did not reply on 0x60 — will use power/presence fallback.")
            else:
                print(f"  DDC/CI: VCP 0x60 = 0x{vcp:02X}  "
                      f"(expected: "
                      f"{'0x%02X' % EXPECTED_INPUT_VCP if EXPECTED_INPUT_VCP is not None else 'auto-learn'})")
            probe.release()
    else:
        print(f"  Target '{MONITOR_NAME}' NOT found.")

    print()
    print(f"  Watching for           : {MONITOR_NAME}")
    print(f"  Chrome path            : {CHROME_PATH}")
    print(f"  Opening URL            : http://localhost:{PORT}/tv")
    print(f"  Poll every             : {MONITOR_POLL_INTERVAL}s")
    print(f"  Require active input   : {REQUIRE_ACTIVE_INPUT}")
    print(f"  Expected input VCP     : {EXPECTED_INPUT_VCP}")
    print(f"  Power-state fallback   : {FALLBACK_TO_POWER_STATE}")
    print()
    print("  Press Ctrl+C to stop.")
    print("-" * 54)
    print()

    start_monitor_watcher(
        target_name             = MONITOR_NAME,
        chrome_path             = CHROME_PATH,
        port                    = PORT,
        poll_interval           = MONITOR_POLL_INTERVAL,
        require_active_input    = REQUIRE_ACTIVE_INPUT,
        expected_input_vcp      = EXPECTED_INPUT_VCP,
        fallback_to_power_state = FALLBACK_TO_POWER_STATE,
    )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Stopped.")
