"""
Build JellyfinHomeTheater.exe and its release files.

    python build.py

produces

    dist/JellyfinHomeTheater/                           the program folder
    release/JellyfinHomeTheater-<version>-win64.zip     what users download
    release/latest.json                                 the update manifest

and `python release.py` publishes those two files as a GitHub release, which
is where installed copies look for updates.

Needs PyInstaller plus the app's own packages:
    pip install -r requirements.txt pyinstaller pystray pillow psutil truststore

Safety: releases are public, and the local config.py holds a Jellyfin
password. The build excludes the `config` module outright (the exe loads the
user's file at runtime instead), and afterwards proves it: the module must not
be in the bundle and the password must not appear in any shipped file.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from version import __version__, APP_NAME, EXE_NAME, UPDATE_REPO  # noqa: E402

NAME = os.path.splitext(EXE_NAME)[0]
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
RELEASE = os.path.join(ROOT, "release")
APP_DIR = os.path.join(DIST, NAME)
ZIP_NAME = f"{NAME}-{__version__}-win64.zip"


def step(msg):
    print(f"\n=== {msg}")


def version_tuple():
    parts = [int(re.match(r"\d+", p).group()) for p in __version__.split(".")[:3]]
    return tuple(parts + [0] * (4 - len(parts)))


def write_version_resource():
    """The Windows version block (Properties → Details), which also helps
    antivirus reputation a little compared with a bare exe."""
    v = version_tuple()
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Denis Kostadinov'),
      StringStruct('FileDescription', '{APP_NAME}'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', '{EXE_NAME}'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    os.makedirs(BUILD, exist_ok=True)
    path = os.path.join(BUILD, "version_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def run_pyinstaller(version_file):
    def data(rel, dest):
        return ["--add-data", f"{os.path.join(ROOT, rel)}{os.pathsep}{dest}"]

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--windowed",
        "--name", NAME,
        "--icon", os.path.join(ROOT, "static", "home-theater.ico"),
        "--version-file", version_file,
        "--distpath", DIST, "--workpath", BUILD, "--specpath", BUILD,
        *data("templates", "templates"),
        *data("static", "static"),
        *data("config.example.py", "."),
        # Loaded by name at runtime, so the analysis can't see them.
        "--hidden-import", "engineio.async_drivers.threading",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "find_displays",
        "--hidden-import", "find_wled",
        "--hidden-import", "find_ha_lights",
        # The user's settings are read from %LOCALAPPDATA% at runtime and must
        # never be baked in.
        "--exclude-module", "config",
        # Not used by the exe: the threading server is pinned (see app.py),
        # and YouTube runs the separately-updated yt-dlp.exe.
        "--exclude-module", "gevent", "--exclude-module", "geventwebsocket",
        "--exclude-module", "eventlet", "--exclude-module", "tkinter",
        "--exclude-module", "yt_dlp", "--exclude-module", "yt_dlp_ejs",
        "--exclude-module", "numpy",
        "--exclude-module", "usercustomize", "--exclude-module", "sitecustomize",
        # Optional imports elsewhere (engineio's other async drivers, urllib3's
        # pyOpenSSL support, pystray's other backends) drag these in; the app
        # uses none of them. ~20 MB.
        "--exclude-module", "aiohttp", "--exclude-module", "tornado",
        "--exclude-module", "cryptography", "--exclude-module", "bcrypt",
        "--exclude-module", "cffi", "--exclude-module", "setuptools",
        "--exclude-module", "pkg_resources", "--exclude-module", "win32com",
        "--exclude-module", "pythoncom", "--exclude-module", "pywintypes",
        "--exclude-module", "win32api", "--exclude-module", "gi",
        "--exclude-module", "Xlib", "--exclude-module", "AppKit",
        os.path.join(ROOT, "jht_main.py"),
    ]
    subprocess.run(args, check=True, cwd=ROOT)


def local_password():
    """The password in the developer's own config.py, if there is one, so the
    build can prove it didn't leak. Read as text — never imported."""
    try:
        with open(os.path.join(ROOT, "config.py"), encoding="utf-8") as f:
            m = re.search(r'^JELLYFIN_PASSWORD\s*=\s*["\'](.*?)["\']', f.read(), re.M)
        value = m.group(1) if m else ""
        return value if value and value != "your-password" else None
    except OSError:
        return None


def check_no_secrets():
    from PyInstaller.archive.readers import pkg_archive_contents
    exe = os.path.join(APP_DIR, EXE_NAME)
    contents = pkg_archive_contents(exe, recursive=True)
    modules = {os.path.basename(str(n)) for n in contents}
    if "config" in modules or any(str(n) in ("config", "config.pyc") for n in contents):
        raise SystemExit("ABORT: the config module was bundled — it holds your password")

    secret = local_password()
    if secret:
        needle = secret.encode("utf-8")
        for folder, _dirs, files in os.walk(APP_DIR):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as f:
                    if needle in f.read():
                        raise SystemExit(f"ABORT: the Jellyfin password appears in {path}")
    example = os.path.join(APP_DIR, "_internal", "config.example.py")
    with open(example, encoding="utf-8") as f:
        if 'JELLYFIN_PASSWORD = "your-password"' not in f.read():
            raise SystemExit("ABORT: config.example.py is not the sanitized template")
    print(f"ok: no config module in {len(contents)} bundled entries; "
          f"{'password not found in any file' if secret else 'no local password to check'}")


def changelog_notes():
    """This version's section of CHANGELOG.md, for the release notes."""
    try:
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(rf"^## \[?{re.escape(__version__)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def make_release():
    os.makedirs(RELEASE, exist_ok=True)
    zip_path = os.path.join(RELEASE, ZIP_NAME)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _dirs, files in os.walk(APP_DIR):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.join(NAME, os.path.relpath(full, APP_DIR)))
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    manifest = {
        "version":   __version__,
        "url":       f"https://github.com/{UPDATE_REPO}/releases/download/v{__version__}/{ZIP_NAME}",
        "sha256":    h.hexdigest(),
        "size":      os.path.getsize(zip_path),
        "published": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "notes":     changelog_notes(),
    }
    with open(os.path.join(RELEASE, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return zip_path, manifest


def main():
    step(f"Building {APP_NAME} {__version__}")
    shutil.rmtree(APP_DIR, ignore_errors=True)
    run_pyinstaller(write_version_resource())
    step("Checking that no personal settings were bundled")
    check_no_secrets()
    step("Packaging")
    zip_path, manifest = make_release()
    size_mb = manifest["size"] / 1e6
    print(f"{zip_path}  ({size_mb:.1f} MB)\nsha256 {manifest['sha256']}")
    print(f"{os.path.join(RELEASE, 'latest.json')}")
    print(f"\nProgram folder: {APP_DIR}")


if __name__ == "__main__":
    main()
