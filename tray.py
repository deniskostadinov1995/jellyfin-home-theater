"""
Jellyfin Home Theater — the tray icon.

While the program runs it sits in the notification area with the things you
would otherwise need a terminal for: bring the TV screen up, find the phone
remote's address, check for and install updates, switch automatic updates on
or off, edit the settings, restart, quit.
"""

import os
import subprocess
import sys
import threading

import paths
from version import APP_NAME, EXE_NAME, __version__

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)


def _restart():
    """Start a fresh copy that waits for this one to exit, then exit."""
    if paths.FROZEN:
        args = [paths.EXE_PATH, "--wait-pid", str(os.getpid())]
    else:
        args = [sys.executable, os.path.join(paths.APP_DIR, "app.py")]
    subprocess.Popen(args, cwd=paths.APP_DIR, close_fds=True,
                     creationflags=_DETACHED | _NO_WINDOW)
    os._exit(0)


def run(updater, show_tv, remote_url):
    """Show the icon and run its message loop on this thread (blocks)."""
    import pystray
    from PIL import Image

    image = Image.open(os.path.join(paths.RESOURCE_DIR, "static", "home-theater.ico"))

    def check_now(icon, _item):
        threading.Thread(target=updater.check, kwargs={"manual": True},
                         daemon=True, name="trayCheck").start()

    def install_now(icon, _item):
        updater.install("from the tray menu")

    def toggle_auto(icon, _item):
        updater.set_auto(not updater.auto)

    def open_remote(icon, _item):
        import webbrowser
        webbrowser.open(remote_url())

    def edit_settings(icon, _item):
        # Notepad, not os.startfile: .py files are often associated with
        # Python itself, which would run the settings instead of opening them.
        subprocess.Popen(["notepad.exe", paths.CONFIG_PATH])

    def open_folder(icon, _item):
        os.startfile(paths.DATA_DIR)

    def restart(icon, _item):
        icon.stop()
        _restart()

    def quit_app(icon, _item):
        icon.stop()
        os._exit(0)

    Item = pystray.MenuItem
    menu = pystray.Menu(
        Item(f"{APP_NAME} {__version__}", None, enabled=False),
        Item("Show TV screen", lambda icon, item: show_tv(), default=True),
        Item(lambda item: f"Phone remote: {remote_url()}", open_remote),
        pystray.Menu.SEPARATOR,
        Item(lambda item: updater.describe(), None, enabled=False),
        Item("Check for updates", check_now,
             enabled=lambda item: updater.enabled),
        Item(lambda item: f"Install update {updater.latest} now", install_now,
             visible=lambda item: updater.status == "ready"),
        Item("Update automatically", toggle_auto,
             checked=lambda item: updater.auto,
             enabled=lambda item: updater.enabled),
        pystray.Menu.SEPARATOR,
        Item("Edit settings", edit_settings),
        Item("Open settings folder", open_folder),
        Item("Restart", restart),
        Item("Quit", quit_app),
    )
    icon = pystray.Icon("JellyfinHomeTheater", image, APP_NAME, menu)

    def refresh():
        try:
            icon.update_menu()
        except Exception:
            pass
    updater.on_change.append(refresh)
    updater.before_exit.append(icon.stop)
    icon.run()
