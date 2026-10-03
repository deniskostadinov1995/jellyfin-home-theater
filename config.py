# ─────────────────────────────────────────────────────────────────────────────
#  Jellyfin Home Theater — Configuration
#  Edit this file with your Jellyfin server details.
# ─────────────────────────────────────────────────────────────────────────────

# Your Jellyfin server URL (default port is 8096).
JELLYFIN_URL = "http://localhost:8096"

# Jellyfin account the TV browses as.  This drives Continue Watching, Next Up,
# watched checkmarks and resume positions, so use the account you actually
# watch with.
JELLYFIN_USER     = "Jellyfin"
JELLYFIN_PASSWORD = "your-password"          # ← your Jellyfin password

# Flask secret key — change this to any random string
SECRET_KEY = "secret"

# Port the app runs on
PORT = 5050

# ── Playback ──────────────────────────────────────────────────────────────────
# Audio channel ceiling when Jellyfin has to convert the audio track (any time
# playback isn't Direct Play — e.g. an MKV, or DTS/TrueHD audio a browser
# can't decode).  2 = stereo downmix, which every setup can play.  Raise to 6
# for 5.1 if your PC's audio output is configured for surround; Chrome will
# otherwise downmix it anyway.
MAX_AUDIO_CHANNELS = 2

# Bandwidth budget, in bits per second, for any stream Jellyfin has to build.
# This must be set generously: Jellyfin sizes the picture to fit the bitrate it
# is given, and with no budget at all it assumes a dial-up-era 256 kbps and
# scales a 4K film down to 416x177.  120 Mbps is right for a wired LAN or the
# same machine; drop it only if you ever stream this over the internet.
MAX_STREAMING_BITRATE = 120_000_000

# ── Monitor Watcher ───────────────────────────────────────────────────────────
# When this display becomes active the watcher auto-launches the /tv page
# in Chrome kiosk mode on that monitor.
#
# How to find your display name — run in PowerShell:
#   Add-Type -AssemblyName System.Windows.Forms
#   [System.Windows.Forms.Screen]::AllScreens | Select-Object DeviceName, Bounds, Primary
#
# Or just run:  python find_displays.py
#
# The name looks like:  \\.\DISPLAY3
# Set to None to disable the watcher entirely.
MONITOR_NAME = r"\\.\DISPLAY3"       # ← replace with YOUR display name

# Full path to chrome.exe  (try both locations if unsure)
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
# CHROME_PATH = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

# How often to check whether the display is connected (seconds)
MONITOR_POLL_INTERVAL = 5

# ── HDMI input detection (DDC/CI) ─────────────────────────────────────────────
# Many TVs keep their HDMI handshake alive (so the PC always thinks the TV is
# "connected") even when the TV is switched to another input.  When this is
# enabled, the watcher uses a layered model to decide whether the TV is really
# showing OUR HDMI output before launching Chrome:
#
#   1) Input source  (DDC/CI VCP 0x60) — asks "which input are you on?".  Each
#      read is retried over a reused connection, so it is far more reliable than
#      a single probe.  This is the signal that distinguishes "on our input"
#      from "on a different input".
#   2) Power state   (DDC/CI VCP 0xD6) — used only when the TV won't answer 0x60.
#      Tells On vs Standby/Off (not which input), so it avoids launching into a
#      sleeping TV.  Controlled by FALLBACK_TO_POWER_STATE below.
#   3) Display presence — last resort if the TV answers no DDC/CI at all: a TV
#      that is off/unplugged drops out of Windows entirely, which we still catch.
#
# First-run workflow:
#   1) Set REQUIRE_ACTIVE_INPUT = True.
#   2) Leave EXPECTED_INPUT_VCP = None.
#   3) Switch the TV to the HDMI input the PC is on.
#   4) Start the app — it logs a line like:
#        "Learned TV input VCP code = 0x11.
#         Pin it in config.py with  EXPECTED_INPUT_VCP = 0x11"
#   5) Copy that value here so the program does not have to re-learn.
#
# Tip: run  python monitor_watcher.py  to see exactly which registers (0x60 /
# 0xD6) your TV answers and what values it returns.
#
# Typical TV codes for VCP 0x60:
#   0x11 HDMI1   0x12 HDMI2   0x13 HDMI3   0x14 HDMI4
#   0x0F DP1     0x10 DP2
# (Some TVs ignore the VESA spec and return arbitrary codes — that is what the
# auto-learn step above is for.)
#
# If your TV answers nothing at all, leave REQUIRE_ACTIVE_INPUT = False and the
# watcher launches whenever the display is simply present.
REQUIRE_ACTIVE_INPUT = False
EXPECTED_INPUT_VCP   = None   # e.g. 0x11 once auto-learned

# When the TV does not answer input-source reads (0x60) but does answer power
# (0xD6), require the TV to be powered On.  Set False to skip straight to
# display-presence detection instead.
FALLBACK_TO_POWER_STATE = True

# ── USB remote bridge ─────────────────────────────────────────────────────────
# A USB remote is just an HID keyboard, so Windows sends its arrow keys to
# whatever window is in front.  remote_bridge.py fixes that: it recognises the
# remote by *device*, swallows its keypresses so they never reach the app you
# are using, and forwards them to the TV UI instead — the kiosk window never
# needs focus, and your keyboard is untouched.
#
# Find the device id once:
#   python remote_probe.py
# Then run it, and optionally start it at logon:
#   python remote_bridge.py
#   python remote_bridge.py --install
REMOTE_DEVICE = ""          # e.g. "VID_248A&PID_168E"

# Which button does what.  Merged over the defaults in remote_bridge.py, so
# you only need entries you want to change.  Keys are names ("MediaPlayPause")
# or codes (0xB3); actions are the ones the TV understands — up, down, left,
# right, select, back, home, search, toggle_play, play, pause, stop, next,
# previous, seek_back, seek_fwd, vol_up, vol_down, mute, toggle_subs,
# toggle_audio, aspect, fullscreen.  Set an action to None to leave that key
# alone so it reaches Windows normally.
REMOTE_KEYS = {}

# Pass the remote's keys through untouched while no TV page is open, so the
# remote still works as a normal keyboard when you are not watching anything.
REMOTE_ONLY_WHEN_TV_RUNNING = True

# Optional: one remote button that turns the bridging off and on, for when you
# want the remote to act as a plain keyboard.  None disables the toggle.
REMOTE_TOGGLE_KEY = None

# ── YouTube app ───────────────────────────────────────────────────────────────
# Maximum YouTube resolution. 2160 = 4K (best on a 4K TV), 1440 = 2K,
# 1080 = Full HD. YouTube only serves above 1080p in VP9/AV1, which this app
# streams via VP9 (widest hardware-decode support in Chrome). Lower this if
# your PC drops frames at 4K.
YT_MAX_HEIGHT = 2160

# ── WLED ambient lighting (optional) ──────────────────────────────────────────
# Your WLED controllers on the LAN.  One address, or a list of them — the
# Lights tab drives all of them together.  Set to None to hide the tab.
#
# To find them again if the addresses change (they are DHCP), run:
#   python find_wled.py
WLED_IP = []        # e.g. ["192.168.1.60", "192.168.1.61"] - run find_wled.py

# How the Lights control treats SignalRGB.
#
# SignalRGB drives the strip by streaming to it over UDP, which puts WLED into
# "realtime" mode — that is the "another app is controlling the lights" notice
# the WLED app shows, and while it lasts WLED ignores its own on/off and
# brightness.  WLED has a switch for exactly this (`lor`, live override), and
# the Lights tab uses it so you can take the lights back without quitting
# SignalRGB:
#
#   Ambilight  → hand control to SignalRGB (follow the screen)
#   Manual     → ignore the stream, so on/off/brightness work again
#   Off        → ignore the stream and switch the strip off
#
# Leave this True.  Set it False only if you would rather the Lights tab never
# touch the override and simply send on/off (which SignalRGB will overrule
# whenever it is streaming).
WLED_USE_LIVE_OVERRIDE = True

# ── Home Assistant (optional) ─────────────────────────────────────────────────
# Lets the playback automation below reach lights that aren't WLED — the
# living-room ceiling lights, for instance.
#
# To get a token: open Home Assistant → click your user (bottom left) →
# Security → "Long-lived access tokens" → Create token.  Paste it here.  It
# does not expire, so this is a one-time step.
HA_URL   = "http://homeassistant.local:8123"
HA_TOKEN = ""          # ← paste your long-lived access token

# Which lights the playback automation should dim.  Run
#   python find_ha_lights.py
# once the token is set and it will list your light entities so you can paste
# the right ones in here.  An empty list means Home Assistant is not used.
HA_LIGHTS = []         # e.g. ["light.living_room_ceiling"]

# ── Dim the lights when something plays ───────────────────────────────────────
# Takes the lights down when playback starts and brings them back when it
# stops, the way a cinema does.  Needs WLED_IP set.  You can also flip this
# from the phone remote's Lights tab without editing the file.
DIM_ON_PLAYBACK = True

# Brightness (1–255) while something is playing.  0 switches the strips off.
LIGHTS_PLAYING_BRI = 40

# Brightness while paused — enough to find the popcorn without killing the mood.
LIGHTS_PAUSED_BRI = 120

# Brightness once you are back to browsing.  None restores whatever the lights
# were set to before playback started, which is usually what you want.
LIGHTS_IDLE_BRI = None

# Keep SignalRGB's ambilight running while playing, just dimmer.  WLED's master
# brightness scales the incoming stream, so the lights still follow the screen
# at the lower level.  Set False to take the strips off the stream during
# playback and hold them at a plain dim glow instead.
LIGHTS_AMBILIGHT_WHILE_PLAYING = True

# Ceiling lights (the HA_LIGHTS above), as a percentage.  These are separate
# from the WLED numbers because the ceiling and the ambilight want different
# levels: the ambilight stays visible while the ceiling drops right down.
HA_PLAYING_PCT = 15    # dimmed, not off, so the room is still navigable
HA_PAUSED_PCT  = 50    # up part-way when you pause
HA_IDLE_PCT    = None  # None restores whatever they were before playback
