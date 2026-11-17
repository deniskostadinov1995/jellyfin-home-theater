# Changelog

Each section here becomes the notes of its GitHub release (build.py reads it),
and the installed program shows them when an update is ready.

## 1.0.3

- The remote works on the TV screen right away: the TV UI now takes the
  keyboard focus when it opens and whenever the remote's button asks for it.
  Windows was refusing the app's request (only the program you last used may
  bring a window to the front), so the remote's arrow keys went to whatever
  window had focus — SignalRGB, for example — until the TV screen was clicked.

## 1.0.2

- Updates hand over faster and more cleanly: the old version now exits within
  two seconds (it could linger while its tray icon shut down), and the new
  version isn't started until the old one has let go of the app's port —
  Windows otherwise lets both answer for a moment.

## 1.0.1

- Fixed: restarting itself — after an update, or the remote's button starting
  the app — could leave the new copy running the old copy's code, and an
  update could stop with the app down. Every copy the program starts is now
  an independent instance (PyInstaller's environment is reset for it).
- The updater can no longer stop on an error dialog: anything unexpected is
  logged to update.log and the previous version is put back and restarted.
- Starting a freshly installed version is retried while antivirus finishes
  scanning it.
- The installer refuses to run from inside another app's package (such as a
  packaged terminal), where Windows would hide the installation from the
  rest of the system; double-click the exe in Explorer instead.
- Unhandled errors go to crash.log instead of a dialog box on the TV.

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
