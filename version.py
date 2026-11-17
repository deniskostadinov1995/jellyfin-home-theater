"""Jellyfin Home Theater — version and update channel.

`__version__` is the single source of truth: build.py stamps it into the exe
and release.py tags the GitHub release with it. Bump it (and add a section to
CHANGELOG.md) for every release.
"""

__version__ = "1.0.0"

APP_NAME = "Jellyfin Home Theater"
EXE_NAME = "JellyfinHomeTheater.exe"

# GitHub repository the installed exe updates itself from. Every release
# carries a `latest.json` manifest (version, download URL, SHA-256), and
# GitHub serves the newest one at a fixed address — see updater.py.
UPDATE_REPO = "deniskostadinov1995/jellyfin-home-theater"
