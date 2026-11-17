# Changelog

Each section here becomes the notes of its GitHub release (build.py reads it),
and the installed program shows them when an update is ready.

## 1.0.0

First release as a Windows program — no Python needed.

- **Installs like any other program**: run the downloaded exe and it installs
  itself per-user (no admin), with Desktop / Start-menu shortcuts, the
  remote-button listener at logon, and an entry in Settings → Apps.
- **Updates itself**: checks GitHub for new versions, downloads and verifies
  them, and installs only when nothing is playing and the TV has been idle.
  A version that fails to start is rolled back automatically. Check, install
  and the auto-update switch are on the TV (Settings in the side menu), on
  the phone remote (⚙ App tab) and in the tray icon menu.
- **Keeps yt-dlp current** on its own, so YouTube keeps working when YouTube
  changes.
- Fixed: every MKV / transcode failed on Jellyfin 12 (it no longer accepts the
  legacy `api_key` URL parameter).
- Fixed: pressing Back on "Up Next" still played the next episode.
- Fixed: the home screen stayed empty if Jellyfin started after the app, and
  Continue Watching never refreshed.
- Fixed: a non-first audio track could silently revert to the first one.
- Fixed: YouTube seeks deep into a video never finished; a refused video now
  says so instead of reconnecting forever.
- The remote's button brings the TV screen back when the app is already
  running; the TV takes over its existing window after a restart instead of
  opening a second one.
