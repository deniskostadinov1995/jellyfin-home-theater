"""
Jellyfin Home Theater — updates.

Two things are kept current:

  1. **The program itself**, from GitHub Releases (UPDATE_REPO in version.py).
     Every release carries `latest.json` — version, zip URL, SHA-256, size,
     notes — and GitHub always serves the newest release's copy at
     /releases/latest/download/latest.json, so checking costs one small
     request and no API quota.

     A running program can't overwrite its own files, so installing works in
     two steps. The new version is downloaded, verified and unpacked next to
     the install (UPDATES_DIR/<version>). The running app then starts *that*
     copy with --apply-update and exits; the new copy waits for it to go,
     swaps the program folder (keeping the old one as a backup), starts the
     new program and checks that it actually comes up. If it doesn't, the old
     folder goes back and that version is never auto-installed again.

  2. **yt-dlp**, the YouTube engine. YouTube breaks old versions every few
     weeks, so the installed program keeps its own yt-dlp.exe in TOOLS_DIR and
     refreshes it from yt-dlp's official releases (verified against their
     published SHA2-256SUMS) instead of waiting for a release of this program.

Updates only apply to the installed exe. Running from source, the status is
simply "disabled" — pull the repository instead.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

import requests

import paths
from version import __version__, APP_NAME, EXE_NAME, UPDATE_REPO

log = logging.getLogger("update")

# JHT_UPDATE_MANIFEST points the updater at another manifest — for testing an
# update end to end without publishing a release.
MANIFEST_URL = (os.environ.get("JHT_UPDATE_MANIFEST")
                or f"https://github.com/{UPDATE_REPO}/releases/latest/download/latest.json")
YTDLP_LATEST = "https://github.com/yt-dlp/yt-dlp/releases/latest"
YTDLP_DOWNLOAD = "https://github.com/yt-dlp/yt-dlp/releases/download/{tag}/{name}"

FIRST_CHECK_DELAY = 60          # after startup, let the TV come up first
CHECK_INTERVAL = 6 * 3600
YTDLP_INTERVAL = 24 * 3600
STARTUP_GRACE = 3 * 60          # never auto-install right after a launch
AUTO_IDLE_SECONDS = 10 * 60     # TV untouched this long (and not playing) → install
HEALTH_TIMEOUT = 60             # how long a new version gets to answer

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def parse_version(text):
    """"v1.2.3" → (1, 2, 3). Unparseable parts count as 0."""
    out = []
    for part in str(text or "").strip().lstrip("vV").split(".")[:3]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _http():
    s = requests.Session()
    s.headers["User-Agent"] = f"JellyfinHomeTheater/{__version__}"
    return s


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _rmtree(path, attempts=6):
    """Delete a folder, riding out the brief locks antivirus scans leave."""
    for i in range(attempts):
        if not os.path.exists(path):
            return True
        try:
            shutil.rmtree(path)
            return True
        except OSError:
            time.sleep(0.5 * (i + 1))
    return not os.path.exists(path)


def _rename(src, dst, attempts=20):
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except OSError:
            time.sleep(0.5)
    return False


# ── settings.json — switches the app owns (not the user's config.py) ─────────
def load_settings():
    try:
        with open(paths.SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    data = load_settings()
    data.update(changes)
    tmp = paths.SETTINGS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, paths.SETTINGS_PATH)
    except OSError as exc:
        log.warning("could not save settings: %s", exc)
    return data


# ═════════════════════════════════════════════════════════════════════════════
#  The running app's side
# ═════════════════════════════════════════════════════════════════════════════
class Updater:
    """Checks, downloads and hands over to the installer, for the running app.

    The app wires in two hooks: `is_idle()` → (bool, reason), which decides
    when an automatic install may interrupt, and `before_exit`, callbacks run
    just before the process exits to let the new version in.
    """

    def __init__(self):
        self._lock = threading.RLock()
        settings = load_settings()
        self.auto = bool(settings.get("autoUpdate", True))
        self.skip_version = settings.get("skipVersion")
        self.enabled = paths.FROZEN
        self.status = "idle" if self.enabled else "disabled"
        self.latest = None
        self.notes = ""
        self.error = None
        self.progress = None            # 0–100 while downloading
        self.last_check = None
        self.manifest = None
        self.staged_dir = None
        self.updated_from = None
        self.started = time.time()
        self.ytdlp_version = None
        self.is_idle = lambda: (True, "")
        self.before_exit = []
        self.on_change = []             # e.g. the tray refreshing its menu
        self.on_ytdlp_updated = None
        self._thread = None

    # ── state ────────────────────────────────────────────────────────────────
    def _set(self, **changes):
        with self._lock:
            for k, v in changes.items():
                setattr(self, k, v)
        for cb in list(self.on_change):
            try:
                cb()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            return {
                "version":         __version__,
                "enabled":         self.enabled,
                "status":          self.status,
                "latest":          self.latest,
                "notes":           self.notes,
                "error":           self.error,
                "progress":        self.progress,
                "lastCheck":       self.last_check,
                "autoUpdate":      self.auto,
                "justUpdatedFrom": self.updated_from,
                "ytDlp":           self.ytdlp_version,
                "repo":            UPDATE_REPO,
            }

    def describe(self):
        """One line for the tray menu."""
        s = self.status
        if s == "disabled":
            return "Updates: running from source"
        if s == "checking":
            return "Checking for updates…"
        if s == "downloading":
            return f"Downloading {self.latest}… {self.progress or 0}%"
        if s == "ready":
            return f"Update {self.latest} ready to install"
        if s == "installing":
            return f"Installing {self.latest}…"
        if s == "error":
            return f"Update check failed: {self.error}"[:120]
        if s == "up-to-date":
            return "Up to date"
        return "Updates: not checked yet"

    def set_auto(self, enabled):
        self._set(auto=bool(enabled))
        save_settings(autoUpdate=bool(enabled))

    # ── check + download ─────────────────────────────────────────────────────
    def check(self, manual=False):
        """Look for a newer release; download it if there is one.

        Returns True when an update is ready to install."""
        if not self.enabled:
            return False
        with self._lock:
            if self.status in ("checking", "downloading", "installing"):
                return False
        self._set(status="checking", error=None)
        try:
            r = _http().get(MANIFEST_URL, timeout=20)
            if r.status_code == 404:
                # No release published yet (or none with a manifest).
                self._set(status="up-to-date", last_check=time.time())
                return False
            r.raise_for_status()
            manifest = r.json()
            latest = str(manifest["version"])
            for key in ("url", "sha256"):
                if not manifest.get(key):
                    raise ValueError(f"release manifest has no {key}")
        except Exception as exc:
            log.warning("update check failed: %s", exc)
            self._set(status="error", error=_short(exc), last_check=time.time())
            return False

        if parse_version(latest) <= parse_version(__version__):
            self._set(status="up-to-date", latest=latest, last_check=time.time(),
                      notes=manifest.get("notes") or "")
            return False

        log.info("update available: %s → %s", __version__, latest)
        self._set(latest=latest, notes=manifest.get("notes") or "",
                  last_check=time.time(), manifest=manifest)
        return self._download(manifest)

    def _download(self, manifest):
        version = str(manifest["version"])
        stage = os.path.join(paths.UPDATES_DIR, version)
        if os.path.isfile(os.path.join(stage, EXE_NAME)) and \
                os.path.isfile(os.path.join(stage, ".verified")):
            self._set(status="ready", staged_dir=stage, progress=None)
            return True

        os.makedirs(paths.UPDATES_DIR, exist_ok=True)
        zip_path = os.path.join(paths.UPDATES_DIR, f"{version}.zip")
        part = zip_path + ".part"
        self._set(status="downloading", progress=0)
        try:
            with _http().get(manifest["url"], stream=True, timeout=30) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or manifest.get("size") or 0)
                done = 0
                with open(part, "wb") as f:
                    for block in r.iter_content(256 * 1024):
                        f.write(block)
                        done += len(block)
                        if total:
                            pct = int(done * 100 / total)
                            if pct != self.progress:
                                self._set(progress=pct)
            if manifest.get("size") and os.path.getsize(part) != int(manifest["size"]):
                raise ValueError("download is the wrong size")
            digest = _sha256(part)
            if digest.lower() != str(manifest["sha256"]).lower():
                raise ValueError("download failed its SHA-256 check")
            os.replace(part, zip_path)

            # Unpack beside the install, then find the folder holding the exe
            # (release zips carry a top-level folder).
            partial = stage + ".partial"
            _rmtree(partial)
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(partial)
            root = _find_app_root(partial)
            if not root:
                raise ValueError(f"{EXE_NAME} is not in the download")
            _rmtree(stage)
            if not _rename(root, stage):
                raise OSError("could not stage the update")
            _rmtree(partial)
            with open(os.path.join(stage, ".verified"), "w") as f:
                f.write(digest)
            try:
                os.remove(zip_path)
            except OSError:
                pass
        except Exception as exc:
            log.warning("update download failed: %s", exc)
            for leftover in (part,):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            self._set(status="error", error=_short(exc), progress=None)
            return False

        log.info("update %s downloaded and verified", version)
        self._set(status="ready", staged_dir=stage, progress=None)
        return True

    # ── install ──────────────────────────────────────────────────────────────
    def install(self, reason="manual"):
        """Hand over to the downloaded version, which installs itself. The
        process exits shortly afterwards. Returns False if nothing is ready."""
        with self._lock:
            if self.status != "ready" or not self.staged_dir:
                return False
            stage = self.staged_dir
            self.status = "installing"
        exe = os.path.join(stage, EXE_NAME)
        args = [exe, "--apply-update",
                "--target", paths.APP_DIR,
                "--wait-pid", str(os.getpid()),
                "--from-version", __version__]
        log.info("installing %s (%s)", self.latest, reason)
        try:
            subprocess.Popen(args, cwd=stage, close_fds=True,
                             creationflags=_DETACHED | _NO_WINDOW)
        except OSError as exc:
            log.error("could not start the installer: %s", exc)
            self._set(status="error", error=_short(exc))
            return False
        self._set(status="installing")
        timer = threading.Timer(1.5, self._exit_for_update)
        timer.daemon = True
        timer.start()
        return True

    def _exit_for_update(self):
        """Get out of the way fast: the installer waits for this process, and
        the new version wants the same port. Clean-up gets two seconds, then
        the process ends regardless — a tray icon or log handler that hangs
        must not keep the old version alive next to the new one."""
        def cleanup():
            for cb in list(self.before_exit):
                try:
                    cb()
                except Exception:
                    pass
            for h in logging.getLogger().handlers:
                try:
                    h.flush()
                except Exception:
                    pass
        t = threading.Thread(target=cleanup, daemon=True, name="exitCleanup")
        t.start()
        t.join(2.0)
        os._exit(0)

    # ── background ───────────────────────────────────────────────────────────
    def start(self):
        if not self.enabled or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="updater")
        self._thread.start()

    def _loop(self):
        threading.Thread(target=self._cleanup_later, daemon=True,
                         name="updateCleanup").start()
        # The YouTube engine first: without it the YouTube app can't work at
        # all, so a missing one is fetched straight away.
        next_tool = 0 if not os.path.isfile(ytdlp_exe()) else time.time() + FIRST_CHECK_DELAY
        next_check = time.time() + FIRST_CHECK_DELAY
        while True:
            now = time.time()
            if now >= next_tool and (self.auto or not os.path.isfile(ytdlp_exe())):
                next_tool = now + (YTDLP_INTERVAL if self.refresh_ytdlp() is not False
                                   else 3600)
            if self.auto and now >= next_check:
                self.check()
                next_check = now + CHECK_INTERVAL
            if (self.auto and self.status == "ready"
                    and self.latest != self.skip_version
                    and now - self.started >= STARTUP_GRACE):
                idle, why = self.is_idle()
                if idle:
                    self.install(f"automatic: {why}")
            time.sleep(20)

    def refresh_ytdlp(self):
        """Update yt-dlp.exe if a newer one is out. Returns False on failure."""
        try:
            installed = update_ytdlp()
        except Exception as exc:
            log.warning("yt-dlp update failed: %s", exc)
            return False
        self._set(ytdlp_version=read_ytdlp_stamp())
        if installed and self.on_ytdlp_updated:
            try:
                self.on_ytdlp_updated()
            except Exception:
                pass
        return True

    def _cleanup_later(self):
        """Remove what earlier updates left behind: the downloads that are now
        installed, and the previous program folder kept as a rollback copy.
        Waits a few minutes, because the installer that started this process
        runs from one of those folders until its health check finishes."""
        time.sleep(180)
        current = parse_version(__version__)
        try:
            for name in os.listdir(paths.UPDATES_DIR):
                path = os.path.join(paths.UPDATES_DIR, name)
                base = name.split(".zip")[0].replace(".partial", "")
                if parse_version(base) <= current or name.endswith((".part", ".partial")):
                    if os.path.isdir(path):
                        _rmtree(path)
                    else:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
        except OSError:
            pass
        _rmtree(paths.APP_DIR.rstrip("\\/") + ".old")


def _find_app_root(folder):
    if os.path.isfile(os.path.join(folder, EXE_NAME)):
        return folder
    for name in os.listdir(folder):
        sub = os.path.join(folder, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, EXE_NAME)):
            return sub
    return None


def _short(exc):
    text = str(exc) or type(exc).__name__
    return text if len(text) < 160 else text[:157] + "…"


# ═════════════════════════════════════════════════════════════════════════════
#  yt-dlp.exe
# ═════════════════════════════════════════════════════════════════════════════
def ytdlp_exe():
    return os.path.join(paths.TOOLS_DIR, "yt-dlp.exe")


def read_ytdlp_stamp():
    try:
        with open(ytdlp_exe() + ".version", "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def update_ytdlp():
    """Fetch the latest official yt-dlp.exe if ours is older or missing.

    Returns the version installed, or None when already current. The exe is
    checked against the SHA2-256SUMS file published with the same release.
    """
    s = _http()
    r = s.head(YTDLP_LATEST, allow_redirects=False, timeout=20)
    tag = (r.headers.get("Location") or "").rstrip("/").rsplit("/", 1)[-1]
    if not tag or tag == "latest":
        raise ValueError("could not find the latest yt-dlp release")
    if tag == read_ytdlp_stamp() and os.path.isfile(ytdlp_exe()):
        return None

    sums = s.get(YTDLP_DOWNLOAD.format(tag=tag, name="SHA2-256SUMS"), timeout=30)
    sums.raise_for_status()
    expected = None
    for line in sums.text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == "yt-dlp.exe":
            expected = parts[0].lower()
    if not expected:
        raise ValueError("yt-dlp.exe is not in the release checksums")

    os.makedirs(paths.TOOLS_DIR, exist_ok=True)
    tmp = ytdlp_exe() + ".download"
    with s.get(YTDLP_DOWNLOAD.format(tag=tag, name="yt-dlp.exe"), stream=True,
               timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for block in r.iter_content(256 * 1024):
                f.write(block)
    if _sha256(tmp) != expected:
        os.remove(tmp)
        raise ValueError("yt-dlp.exe failed its SHA-256 check")
    # A search may be running the old binary; Windows refuses to replace a
    # running exe, so wait for it rather than give up.
    if not _rename(tmp, ytdlp_exe(), attempts=60):
        os.remove(tmp)
        raise OSError("yt-dlp.exe is in use")
    with open(ytdlp_exe() + ".version", "w", encoding="utf-8") as f:
        f.write(tag)
    log.info("yt-dlp updated to %s", tag)
    return tag


# ═════════════════════════════════════════════════════════════════════════════
#  The installer side: `JellyfinHomeTheater.exe --apply-update …`
# ═════════════════════════════════════════════════════════════════════════════
def _arg(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _ulog(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        with open(paths.UPDATE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _processes_of(exe_path, exclude=()):
    import psutil
    want = os.path.normcase(os.path.abspath(exe_path))
    found = []
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if p.pid in exclude:
                continue
            exe = p.info.get("exe")
            if exe and os.path.normcase(exe) == want:
                found.append(p)
        except Exception:
            continue
    return found


def _stop(processes, timeout=10):
    import psutil
    for p in processes:
        try:
            p.terminate()
        except Exception:
            pass
    _gone, alive = psutil.wait_procs(processes, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except Exception:
            pass
    psutil.wait_procs(alive, timeout=5)


def _start(exe, *args):
    """Start the program detached. A freshly written exe is often still being
    scanned by antivirus, which refuses to run it for a moment ("Access is
    denied") — so retry for a while rather than give up. Never raises."""
    for attempt in range(10):
        try:
            return subprocess.Popen([exe, *args], cwd=os.path.dirname(exe),
                                    close_fds=True, creationflags=_DETACHED | _NO_WINDOW)
        except OSError as exc:
            _ulog(f"could not start {os.path.basename(exe)} {' '.join(args)}: {exc}"
                  + (" — retrying" if attempt < 9 else ""))
            time.sleep(1.5)
    return None


def _wait_port_free(port, timeout=15):
    """Wait until nothing listens on the app's port any more. Windows lets a
    second server bind a port the first still holds, and both then answer —
    so the old version must be completely gone before the new one starts.
    Returns the pids still listening (empty when free)."""
    import psutil
    deadline = time.time() + timeout
    while True:
        try:
            holders = {c.pid for c in psutil.net_connections(kind="tcp")
                       if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port
                       and c.pid}
        except Exception:
            return set()
        if not holders or time.time() >= deadline:
            return holders
        time.sleep(0.5)


def _healthy(port, version, timeout=HEALTH_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/api/update/status", timeout=3)
            if r.ok and r.json().get("version") == version:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def apply_update_main(argv):
    """Install the version this process is (it runs from the staged folder)
    over --target, then start it and make sure it comes up. Whatever goes
    wrong, the program folder ends up holding a working version and that
    version is running — an exception never escapes into a dialog."""
    import traceback
    try:
        return _apply_update(argv)
    except BaseException:
        _ulog("update failed unexpectedly:\n" + traceback.format_exc())
        target = os.path.abspath(_arg(argv, "--target", paths.DEFAULT_INSTALL_DIR))
        backup = target.rstrip("\\/") + ".old"
        exe = os.path.join(target, EXE_NAME)
        try:
            if not os.path.isfile(exe) and os.path.isdir(backup):
                _rmtree(target)
                _rename(backup, target)
                _ulog("restored the previous version")
            if os.path.isfile(exe) and not _processes_of(exe, exclude={os.getpid()}):
                _start(exe)
                _start(exe, "--listener")
        except BaseException:
            _ulog("recovery failed:\n" + traceback.format_exc())
        return 1


def _apply_update(argv):
    import psutil

    paths.ensure_dirs()
    target = os.path.abspath(_arg(argv, "--target", paths.DEFAULT_INSTALL_DIR))
    wait_pid = int(_arg(argv, "--wait-pid", "0") or 0)
    from_version = _arg(argv, "--from-version", "?")
    source = paths.APP_DIR
    target_exe = os.path.join(target, EXE_NAME)
    backup = target.rstrip("\\/") + ".old"
    try:
        port = int(paths.setting("PORT", 5050))
    except Exception:
        port = 5050
    _ulog(f"installing {__version__} over {from_version} in {target}")

    if os.path.normcase(source) == os.path.normcase(target):
        _ulog("refusing: the update would replace the folder it is running from")
        return 2

    # 1. Let the app that asked for this exit, then stop anything else running
    #    from the program folder (the button listener), noting what to restart.
    if wait_pid:
        try:
            p = psutil.Process(wait_pid)
            _gone, alive = psutil.wait_procs([p], timeout=30)
            if alive:
                _stop(alive)
        except psutil.NoSuchProcess:
            pass
    others = _processes_of(target_exe, exclude={os.getpid()})
    restart_listener = any("--listener" in (p.info.get("cmdline") or []) for p in others)
    if others:
        _ulog(f"stopping {len(others)} other process(es) running from the program folder")
        _stop(others)
    holders = _wait_port_free(port)
    ours = []
    for pid in holders:
        try:
            proc = psutil.Process(pid)
            if os.path.basename(proc.exe() or "").lower() == EXE_NAME.lower():
                ours.append(proc)
            else:
                _ulog(f"port {port} is held by another program (pid {pid}, {proc.name()})")
        except Exception:
            continue
    if ours:
        _ulog(f"an old copy still holds port {port} (pid {[p.pid for p in ours]}) — stopping it")
        _stop(ours)

    # 2. Swap the folders, keeping the old one until the new one proves itself.
    _rmtree(backup)
    if os.path.isdir(target) and not _rename(target, backup):
        _ulog("could not move the current program aside (files in use?) — update abandoned")
        _restart_old(target_exe, restart_listener)
        return 1
    try:
        shutil.copytree(source, target,
                        ignore=shutil.ignore_patterns(".verified"))
    except Exception as exc:
        _ulog(f"copy failed: {exc} — rolling back")
        _rmtree(target)
        _rename(backup, target)
        _restart_old(target_exe, restart_listener)
        return 1

    # 3. Start the new version and wait for it to answer. (Started with this
    #    process's environment, which sanitize_environment() has cleaned, so
    #    each is a fresh, independent instance.)
    _start(target_exe, "--updated-from", from_version)
    if restart_listener:
        _start(target_exe, "--listener")
    if _healthy(port, __version__):
        _ulog(f"{__version__} is up — update complete")
        _rmtree(backup)
        return 0

    # 4. It didn't come up: put the old version back, and never auto-install
    #    this one again.
    _ulog(f"{__version__} did not start within {HEALTH_TIMEOUT}s — rolling back to {from_version}")
    _stop(_processes_of(target_exe, exclude={os.getpid()}))
    failed = target.rstrip("\\/") + ".failed"
    _rmtree(failed)
    if not _rename(target, failed):
        _rmtree(target)
    _rename(backup, target)
    save_settings(skipVersion=__version__)
    _restart_old(target_exe, restart_listener)
    _rmtree(failed)
    return 1


def _restart_old(target_exe, restart_listener):
    if os.path.isfile(target_exe):
        _start(target_exe)
        if restart_listener:
            _start(target_exe, "--listener")
