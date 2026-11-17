"""
Jellyfin Home Theater — entry point of JellyfinHomeTheater.exe.

    JellyfinHomeTheater.exe                  the app: TV + phone-remote server, tray icon
    JellyfinHomeTheater.exe --launch         start everything (app + SignalRGB), or bring
                                             the TV screen back if it is already running
                                             — what the Desktop / Start-menu shortcut runs
    JellyfinHomeTheater.exe --listener       wait for the remote's button (runs at logon)
    JellyfinHomeTheater.exe --install [--migrate-from <folder>]
    JellyfinHomeTheater.exe --uninstall [--quiet]

Setup tools (they open a console):
    --find-displays  --find-wled  --find-ha-lights  --detect-button

Used internally: --apply-update … (the updater), --updated-from <version>,
--wait-pid <pid> (restart), --portable (run a download without installing).

From source, `python app.py` is still the way to run the app.
"""

import sys

TOOLS = {
    "--find-displays":  ("find_displays", "main"),
    "--find-wled":      ("find_wled", "main"),
    "--find-ha-lights": ("find_ha_lights", "main"),
    "--detect-button":  ("remote_launcher", "detect_mode"),
}

ERROR_ALREADY_EXISTS = 183


def _console():
    """Give the windowed exe a console for the command-line tools: the one it
    was started from, or a new window. Returns True when attached to an
    existing one."""
    import ctypes
    k32 = ctypes.windll.kernel32
    attached = bool(k32.AttachConsole(-1))          # ATTACH_PARENT_PROCESS
    if not attached:
        k32.AllocConsole()
    sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stderr = sys.stdout
    try:
        sys.stdin = open("CONIN$", "r", encoding="utf-8")
    except OSError:
        pass
    return attached


def _trust_windows_certificates():
    """Verify HTTPS against the Windows certificate store. Antivirus HTTPS
    scanning (AVG, Avast…) re-signs every site with a root that only Windows
    knows about, and update checks would otherwise fail on such machines."""
    try:
        import truststore
        truststore.inject_into_ssl()
    except Exception:
        pass


def _wait_for(pid):
    try:
        import psutil
        psutil.Process(int(pid)).wait(timeout=30)
    except Exception:
        pass


def _only_instance(name):
    """Hold a named mutex for the life of the process; False if another
    process already holds it."""
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, name)
    _only_instance.handle = handle          # keep it alive
    return k32.GetLastError() != ERROR_ALREADY_EXISTS


def _arg(argv, name):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _crash_log(exc_text):
    """Unhandled errors go to crash.log instead of PyInstaller's modal
    "Unhandled exception" dialog, which would sit on the TV until clicked."""
    try:
        import time
        import paths
        paths.ensure_dirs()
        import os
        with open(os.path.join(paths.DATA_DIR, "crash.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(sys.argv[1:])}\n"
                    f"{exc_text}\n")
    except Exception:
        pass


def main():
    try:
        _main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        _crash_log(traceback.format_exc())
        sys.exit(1)


def _main():
    argv = sys.argv[1:]
    import safe_console
    safe_console.enable()
    import paths
    if paths.FROZEN:
        paths.sanitize_environment()
        _trust_windows_certificates()

    if "--apply-update" in argv:
        import updater
        sys.exit(updater.apply_update_main(argv))
    if "--uninstall" in argv:
        import installer
        sys.exit(installer.uninstall(quiet="--quiet" in argv))
    if "--install" in argv:
        import installer
        sys.exit(installer.install_main(argv))

    for flag, (module, func) in TOOLS.items():
        if flag in argv:
            attached = _console()
            paths.load_config()
            import importlib
            try:
                getattr(importlib.import_module(module), func)()
            except KeyboardInterrupt:
                pass
            if not attached:
                input("\nPress Enter to close…")
            return

    if "--listener" in argv:
        paths.load_config()
        import remote_launcher
        remote_launcher.listen_mode()
        return
    if "--launch" in argv:
        paths.load_config()
        import remote_launcher
        remote_launcher.ensure_running()
        return

    # ── The app itself ───────────────────────────────────────────────────────
    if _arg(argv, "--wait-pid"):
        _wait_for(_arg(argv, "--wait-pid"))       # a restart: let the old one go
    if (paths.FROZEN and not paths.is_installed()
            and not {"--portable", "--updated-from"} & set(argv)):
        import installer
        if installer.offer_install():
            return
    if not _only_instance("Local\\JellyfinHomeTheater.App"):
        # Already running: bring its TV screen up rather than start another.
        paths.load_config()
        import remote_launcher
        remote_launcher.show_tv_ui()
        return

    import app
    app.main(tray=paths.FROZEN, updated_from=_arg(argv, "--updated-from"))


if __name__ == "__main__":
    main()
