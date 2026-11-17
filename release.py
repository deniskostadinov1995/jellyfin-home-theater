"""
Publish a built release to GitHub, where installed copies look for updates.

    python release.py            publish what build.py produced
    python release.py --build    build first, then publish

Creates the GitHub release `v<version>` with two assets: the program zip and
`latest.json` (the update manifest). GitHub serves the newest release's
manifest at /releases/latest/download/latest.json, which is the address the
installed program checks — so publishing is all an update needs.

Needs the GitHub CLI (`gh`), logged in with access to the repository.
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from version import __version__, APP_NAME, UPDATE_REPO  # noqa: E402

RELEASE = os.path.join(ROOT, "release")
TAG = f"v{__version__}"


def run(*args, check=True, capture=False):
    return subprocess.run(list(args), check=check, cwd=ROOT, text=True,
                          capture_output=capture)


def main():
    if "--build" in sys.argv:
        run(sys.executable, os.path.join(ROOT, "build.py"))

    manifest_path = os.path.join(RELEASE, "latest.json")
    if not os.path.isfile(manifest_path):
        raise SystemExit("No release/latest.json — run build.py first.")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("version") != __version__:
        raise SystemExit(f"release/ holds {manifest.get('version')}, but version.py says "
                         f"{__version__} — run build.py again.")
    zip_path = os.path.join(RELEASE, os.path.basename(manifest["url"]))
    if not os.path.isfile(zip_path):
        raise SystemExit(f"Missing {zip_path} — run build.py again.")

    existing = run("gh", "release", "view", TAG, "--repo", UPDATE_REPO,
                   check=False, capture=True)
    if existing.returncode == 0:
        raise SystemExit(f"{TAG} is already published. Bump __version__ in version.py "
                         f"for a new release.")

    status = run("git", "status", "--porcelain", check=False, capture=True)
    if status.returncode == 0 and status.stdout.strip():
        print("warning: the working tree has uncommitted changes — the release is "
              "built from them, but the repository won't show them until you commit.")

    notes = manifest.get("notes") or f"{APP_NAME} {__version__}"
    notes += ("\n\n---\n**Install:** download the zip below, extract it, run "
              "`JellyfinHomeTheater.exe` and choose *Yes*. Installed copies update "
              "themselves.")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as f:
        f.write(notes)
        notes_file = f.name
    try:
        run("gh", "release", "create", TAG, zip_path, manifest_path,
            "--repo", UPDATE_REPO,
            "--title", f"{APP_NAME} {__version__}",
            "--notes-file", notes_file,
            "--latest")
    finally:
        os.remove(notes_file)
    print(f"\nPublished {TAG}: https://github.com/{UPDATE_REPO}/releases/tag/{TAG}")


if __name__ == "__main__":
    main()
