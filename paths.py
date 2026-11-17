"""
Jellyfin Home Theater — where everything lives.

The same code runs two ways:

  * From source (`python app.py`): everything stays in the project folder,
    exactly as before — config.py, the token cache and the logs.
  * As the installed exe: the program folder holds only the program, so an
    update can replace it wholesale. Everything that belongs to the user —
    config.py, the token cache, settings, logs, downloaded tools — lives in
    %LOCALAPPDATA%\\Jellyfin Home Theater and survives every update.
"""

import importlib.util
import os
import shutil
import sys

from version import APP_NAME, EXE_NAME

FROZEN = bool(getattr(sys, "frozen", False))

# Read-only files shipped with the program: templates, static, config example.
RESOURCE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))

# The folder the program runs from (the install folder for the exe).
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if FROZEN
           else os.path.dirname(os.path.abspath(__file__)))
EXE_PATH = os.path.abspath(sys.executable) if FROZEN else None

_LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")

DATA_DIR = os.path.join(_LOCAL, APP_NAME) if FROZEN else APP_DIR
UPDATES_DIR = os.path.join(_LOCAL, APP_NAME, "updates")
TOOLS_DIR = os.path.join(_LOCAL, APP_NAME, "tools")
DEFAULT_INSTALL_DIR = os.path.join(_LOCAL, "Programs", APP_NAME)
INSTALLED_EXE = os.path.join(DEFAULT_INSTALL_DIR, EXE_NAME)

CONFIG_PATH = os.path.join(DATA_DIR, "config.py")
CONFIG_EXAMPLE = os.path.join(RESOURCE_DIR, "config.example.py")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
TOKEN_CACHE = os.path.join(DATA_DIR, ".jellyfin_session.json")
LOG_PATH = os.path.join(DATA_DIR, "app.log")
UPDATE_LOG = os.path.join(DATA_DIR, "update.log")
LAUNCHER_LOG = os.path.join(DATA_DIR, "remote_launcher.log")


def ensure_dirs():
    for d in (DATA_DIR, UPDATES_DIR, TOOLS_DIR) if FROZEN else (DATA_DIR,):
        os.makedirs(d, exist_ok=True)


def is_installed():
    """True when this exe is the installed copy (not a download being run)."""
    return FROZEN and os.path.normcase(EXE_PATH) == os.path.normcase(INSTALLED_EXE)


def load_config():
    """Make `import config` find the user's config.py, creating it from
    config.example.py on first run.

    Everything in the app reads settings with `from config import X`. From
    source that is simply the project's config.py. The exe can't use the
    copy it was built with — that would bake one person's settings (and
    password) into the program — so it loads the file from DATA_DIR and
    registers it under the same module name.
    """
    if "config" in sys.modules:
        return sys.modules["config"]
    ensure_dirs()
    if not os.path.isfile(CONFIG_PATH) and os.path.isfile(CONFIG_EXAMPLE):
        shutil.copyfile(CONFIG_EXAMPLE, CONFIG_PATH)
    if not FROZEN:
        import config  # noqa: F401  — the project folder is on sys.path
        return sys.modules["config"]
    spec = importlib.util.spec_from_file_location("config", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["config"] = module
    spec.loader.exec_module(module)
    return module


def setting(name, default=None):
    """One value from config.py, or `default` when the file predates it."""
    return getattr(load_config(), name, default)
