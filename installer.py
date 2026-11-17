"""
Jellyfin Home Theater — install and uninstall for the exe.

Installs per-user, so neither installing nor updating ever needs admin rights:

    %LOCALAPPDATA%\\Programs\\Jellyfin Home Theater\\   the program (replaced by updates)
    %LOCALAPPDATA%\\Jellyfin Home Theater\\             config.py, logs, downloads (kept)

and sets up:

  * Desktop + Start-menu shortcut "Jellyfin Home Theater" → `--launch`
    (start everything, or bring the TV screen back if it is already running)
  * Startup shortcut "Jellyfin Home Theater Remote" → `--listener`
    (waits at logon for the remote's button)
  * an entry in Settings → Apps, so it uninstalls like any other program.

Run the downloaded exe and it offers to install itself; `--install` does it
without asking. `--migrate-from <folder>` brings config.py and the cached
Jellyfin session over from a source checkout and retires the old
pythonw-based launcher there.
"""

import os
import shutil
import subprocess
import sys
import time
import winreg

import paths
from version import APP_NAME, EXE_NAME, __version__

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\JellyfinHomeTheater"
PUBLISHER = "Denis Kostadinov"

MB_YESNOCANCEL, MB_YESNO, MB_OK = 0x3, 0x4, 0x0
MB_ICONQUESTION, MB_ICONINFO, MB_ICONERROR = 0x20, 0x40, 0x10
IDYES, IDNO = 6, 7


def message(text, flags=MB_OK | MB_ICONINFO):
    import ctypes
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)


def _log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line)
    try:
        paths.ensure_dirs()
        with open(os.path.join(paths.DATA_DIR, "install.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── Shortcuts ────────────────────────────────────────────────────────────────
def _special(name):
    """Desktop / Programs / Startup folder of the current user."""
    appdata = os.environ.get("APPDATA", "")
    if name == "Desktop":
        return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if name == "Programs":
        return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs")
    if name == "Startup":
        return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")
    raise ValueError(name)


def _ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def make_shortcut(lnk, target, args="", workdir="", icon="", description="",
                  minimized=False):
    script = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(lnk)});"
        f"$s.TargetPath={_ps_quote(target)};"
        f"$s.Arguments={_ps_quote(args)};"
        f"$s.WorkingDirectory={_ps_quote(workdir)};"
        f"$s.IconLocation={_ps_quote(icon or target)};"
        f"$s.Description={_ps_quote(description)};"
        f"$s.WindowStyle={7 if minimized else 1};"
        "$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, creationflags=_NO_WINDOW, capture_output=True)


def shortcut_paths():
    return {
        "desktop": os.path.join(_special("Desktop"), f"{APP_NAME}.lnk"),
        "menu":    os.path.join(_special("Programs"), f"{APP_NAME}.lnk"),
        "startup": os.path.join(_special("Startup"), f"{APP_NAME} Remote.lnk"),
    }


# Shortcuts left by the pre-exe, run-from-source setup.
LEGACY_STARTUP = ("PlexRemoteLauncher.lnk", "JellyfinRemoteLauncher.lnk")


def _keep_previous(lnk):
    """Save a shortcut we are about to overwrite (once), so a setup from
    before the exe can be put back by hand."""
    if not os.path.isfile(lnk):
        return
    keep = os.path.join(paths.DATA_DIR, "previous-shortcuts")
    dest = os.path.join(keep, os.path.basename(lnk))
    if os.path.exists(dest):
        return
    os.makedirs(keep, exist_ok=True)
    shutil.copy2(lnk, dest)
    _log(f"kept a copy of the previous {os.path.basename(lnk)} in {keep}")


def create_shortcuts(exe, listener=True):
    links = shortcut_paths()
    for lnk in links.values():
        _keep_previous(lnk)
    workdir = os.path.dirname(exe)
    make_shortcut(links["desktop"], exe, "--launch", workdir,
                  description="Start Jellyfin Home Theater, or bring the TV screen back")
    make_shortcut(links["menu"], exe, "--launch", workdir,
                  description="Start Jellyfin Home Theater, or bring the TV screen back")
    if listener:
        make_shortcut(links["startup"], exe, "--listener", workdir, minimized=True,
                      description="Waits for the remote's button to start Jellyfin Home Theater")


def remove_legacy_startup():
    """Move the old pythonw launcher shortcuts out of Startup (kept in the
    data folder, so the old setup can be put back by hand)."""
    keep = os.path.join(paths.DATA_DIR, "previous-shortcuts")
    for name in LEGACY_STARTUP:
        lnk = os.path.join(_special("Startup"), name)
        if os.path.isfile(lnk):
            os.makedirs(keep, exist_ok=True)
            shutil.move(lnk, os.path.join(keep, name))
            _log(f"moved the old Startup shortcut {name} to {keep}")


# ── Registry: Settings → Apps ────────────────────────────────────────────────
def register_uninstall(install_dir):
    exe = os.path.join(install_dir, EXE_NAME)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
        values = {
            "DisplayName": APP_NAME,
            "DisplayVersion": __version__,
            "Publisher": PUBLISHER,
            "InstallLocation": install_dir,
            "DisplayIcon": exe,
            "UninstallString": f'"{exe}" --uninstall',
            "QuietUninstallString": f'"{exe}" --uninstall --quiet',
            "URLInfoAbout": "https://github.com/deniskostadinov1995/jellyfin-home-theater",
        }
        for name, value in values.items():
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        for name in ("NoModify", "NoRepair"):
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 1)
        try:
            size = sum(os.path.getsize(os.path.join(r, f))
                       for r, _d, fs in os.walk(install_dir) for f in fs) // 1024
            winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD, size)
        except OSError:
            pass


def refresh_registration():
    """Called by the installed app at startup: keep the version shown in
    Settings → Apps current after an update."""
    if not paths.is_installed():
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            current, _ = winreg.QueryValueEx(k, "DisplayVersion")
            if current != __version__:
                winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, __version__)
    except OSError:
        register_uninstall(paths.APP_DIR)


# ── Processes ────────────────────────────────────────────────────────────────
def _stop_processes(predicate, why):
    import psutil
    victims = []
    for p in psutil.process_iter(["pid", "exe", "cmdline", "cwd"]):
        if p.pid == os.getpid():
            continue
        try:
            if predicate(p):
                victims.append(p)
        except Exception:
            continue
    for p in victims:
        _log(f"stopping {why}: pid {p.pid} {' '.join(p.info.get('cmdline') or [])[:120]}")
        try:
            p.terminate()
        except Exception:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=10)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    return victims


def _is_installed_exe(p):
    exe = p.info.get("exe")
    return exe and os.path.normcase(exe) == os.path.normcase(paths.INSTALLED_EXE)


def _is_source_app(folder):
    """The pythonw app.py / remote_launcher.py of a source checkout."""
    folder = os.path.normcase(os.path.abspath(folder))

    def check(p):
        cmd = p.info.get("cmdline") or []
        if not cmd or "python" not in os.path.basename(cmd[0]).lower():
            return False
        scripts = [c for c in cmd[1:] if c.lower().endswith((".py", ".pyw"))]
        for s in scripts:
            name = os.path.basename(s).lower()
            if name not in ("app.py", "remote_launcher.py", "remote_bridge.py"):
                continue
            full = s if os.path.isabs(s) else os.path.join(p.info.get("cwd") or "", s)
            if os.path.normcase(os.path.dirname(os.path.abspath(full))) == folder:
                return True
        return False
    return check


def _is_app_process(p):
    """The app itself, as opposed to the button listener or a helper."""
    cmd = [c.lower() for c in (p.info.get("cmdline") or [])]
    if any(os.path.basename(c) == "app.py" for c in cmd):
        return True
    if os.path.basename(p.info.get("exe") or "").lower() == EXE_NAME.lower():
        return not any(flag in cmd for flag in
                       ("--listener", "--apply-update", "--install", "--launch"))
    return False


# ── Install ──────────────────────────────────────────────────────────────────
def _copy_program(src, dst):
    """Copy the program folder into place, keeping the old copy until the new
    one is complete."""
    from updater import _rename, _rmtree
    backup = dst.rstrip("\\/") + ".old"
    _rmtree(backup)
    if os.path.isdir(dst) and not _rename(dst, backup):
        raise OSError(f"{dst} is in use — close Jellyfin Home Theater and try again")
    try:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".verified"))
    except Exception:
        _rmtree(dst)
        if os.path.isdir(backup):
            _rename(backup, dst)
        raise
    _rmtree(backup)


def migrate(folder):
    """Bring settings and the Jellyfin session over from a source checkout."""
    paths.ensure_dirs()
    moved = []
    for name in ("config.py", ".jellyfin_session.json"):
        src = os.path.join(folder, name)
        dst = os.path.join(paths.DATA_DIR, name)
        if os.path.isfile(src) and not os.path.isfile(dst):
            shutil.copy2(src, dst)
            moved.append(name)
    if moved:
        _log(f"copied {', '.join(moved)} from {folder}")
    return moved


def install(migrate_from=None, start=True):
    src = paths.APP_DIR
    dst = paths.DEFAULT_INSTALL_DIR
    exe = os.path.join(dst, EXE_NAME)
    _log(f"installing {APP_NAME} {__version__} from {src} to {dst}")

    # Anything running from the old install, or from the source checkout we
    # are replacing, has to stop first: the folder is about to be swapped, and
    # two button listeners would both react to the remote.
    stopped = _stop_processes(_is_installed_exe, "the installed copy")
    if migrate_from:
        stopped += _stop_processes(_is_source_app(migrate_from), "the source version")
    # Restart the app afterwards if it was running (not just the listener).
    was_running = any(_is_app_process(p) for p in stopped)

    if os.path.normcase(src) != os.path.normcase(dst):
        _copy_program(src, dst)
    paths.ensure_dirs()
    if migrate_from:
        migrate(migrate_from)
    elif not os.path.isfile(paths.CONFIG_PATH):
        paths.load_config()                     # writes config.py from the example

    create_shortcuts(exe)
    remove_legacy_startup()
    register_uninstall(dst)
    _log("shortcuts and Settings → Apps entry created")

    if start:
        subprocess.Popen([exe, "--listener"], cwd=dst, close_fds=True,
                         creationflags=_DETACHED | _NO_WINDOW)
        if was_running:
            subprocess.Popen([exe], cwd=dst, close_fds=True,
                             creationflags=_DETACHED | _NO_WINDOW)
    _log("install complete")
    return exe


def install_main(argv):
    migrate_from = None
    if "--migrate-from" in argv:
        i = argv.index("--migrate-from")
        if i + 1 < len(argv):
            migrate_from = argv[i + 1]
    try:
        install(migrate_from=migrate_from)
    except Exception as exc:
        _log(f"install failed: {exc}")
        if "--quiet" not in argv:
            message(f"Installation failed:\n\n{exc}", MB_OK | MB_ICONERROR)
        return 1
    return 0


def offer_install():
    """Run from a download (not the installed copy): offer to install.
    Returns True when the caller should exit (installed, or cancelled)."""
    answer = message(
        f"Install {APP_NAME} {__version__} on this PC?\n\n"
        f"Yes — install to {paths.DEFAULT_INSTALL_DIR}, add Desktop and Start "
        f"menu shortcuts, start the remote-button listener at logon, and keep "
        f"it up to date automatically.\n\n"
        f"No — just run it from here this once.",
        MB_YESNOCANCEL | MB_ICONQUESTION)
    if answer == IDYES:
        try:
            exe = install()
        except Exception as exc:
            message(f"Installation failed:\n\n{exc}", MB_OK | MB_ICONERROR)
            return True
        first_run = not os.path.isfile(paths.TOKEN_CACHE)
        subprocess.Popen([exe], cwd=os.path.dirname(exe), close_fds=True,
                         creationflags=_DETACHED | _NO_WINDOW)
        message(
            f"{APP_NAME} is installed and running (look for its icon in the "
            f"notification area).\n\n"
            + ("Next: tray icon → Edit settings, and enter your Jellyfin "
               "server address, user and password.\n\n" if first_run else "")
            + f"Settings and logs are in:\n{paths.DATA_DIR}")
        return True
    return answer != IDNO


# ── Uninstall ────────────────────────────────────────────────────────────────
def uninstall(quiet=False):
    if not quiet and message(
            f"Uninstall {APP_NAME}?\n\nYour settings and logs stay in\n"
            f"{paths.DATA_DIR}\n(delete that folder too if you want them gone).",
            MB_YESNO | MB_ICONQUESTION) != IDYES:
        return 1
    _stop_processes(_is_installed_exe, "the installed copy")
    for lnk in shortcut_paths().values():
        try:
            os.remove(lnk)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    # This exe is in the folder being removed: hand the deletion to cmd,
    # which waits for us to exit first.
    target = paths.DEFAULT_INSTALL_DIR
    subprocess.Popen(
        f'cmd /c ping -n 4 127.0.0.1 >nul & rmdir /s /q "{target}"',
        creationflags=_DETACHED | _NO_WINDOW, close_fds=True)
    _log("uninstalled")
    if not quiet:
        message(f"{APP_NAME} has been uninstalled.")
    return 0
