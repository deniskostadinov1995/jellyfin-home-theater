# 🎬 Jellyfin Home Theater

**A custom smart-TV interface for [Jellyfin](https://jellyfin.org) that runs in a browser on your HTPC and is controlled from your phone.** It also has an ad-free YouTube app, ambient-light automation (WLED / Home Assistant) and USB-remote support.

![Python](https://img.shields.io/badge/python-3.10%2B-3776AB)
![Flask](https://img.shields.io/badge/Flask-SocketIO-000000)
![Jellyfin](https://img.shields.io/badge/Jellyfin-10.10%2B%20%7C%2012-00A4DC)
![Windows](https://img.shields.io/badge/Windows-installer%20%2B%20auto--update-0078D6)
![Platform](https://img.shields.io/badge/kiosk%20helpers-Windows-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

| Page | URL | Purpose |
|------|-----|---------|
| **TV Display** | `/tv` | Fullscreen smart-TV UI for your HDMI-connected display |
| **Phone Remote** | `/remote` | D-pad + media controls + WLED lights, open on your phone |
| **Landing** | `/` | Links to both |

Besides your Jellyfin library, the TV UI has an **Apps row**, which currently holds a built-in **YouTube app** (see below).
Both pages connect via WebSocket, so the remote controls the TV in real time.

### Highlights

- **TV-style navigation**: Continue Watching, Next Up, libraries, search and a pop-out side menu, all driven by a D-pad.
- **Smart playback**: Direct Play → Direct Stream → Transcode, always the cheapest path that works. Video is never re-encoded unless it has to be.
- **Skip Intro / Skip Credits** from Jellyfin's built-in media segments, plus auto "Up Next", instant subtitles and multiple-version support.
- **Ad-free YouTube** through `yt-dlp`, with SponsorBlock, up to 4K and self-healing streams.
- **Ambient lighting**: WLED "ambilight / manual / off" modes that cooperate with SignalRGB, plus cinema-style auto-dimming of WLED strips and Home Assistant ceiling lights.
- **Aspect modes** that remove black bars so screen-capture ambilights stay lit.
- **USB remote bridge**: a cheap HID remote controls the TV UI even while you use the PC for something else.
- **Auto kiosk**: launches the TV UI when the TV switches to the PC's HDMI input (DDC/CI detection).
- **Installs and updates itself** on Windows: one exe, no Python needed, automatic updates that wait until the TV is idle.

---

## Download and install (Windows)

1. Download `JellyfinHomeTheater-<version>-win64.zip` from the
   [latest release](https://github.com/deniskostadinov1995/jellyfin-home-theater/releases/latest)
   and extract it anywhere.
2. Run `JellyfinHomeTheater.exe` and answer **Yes** to install. It installs
   for your user only (no admin rights) to
   `%LOCALAPPDATA%\Programs\Jellyfin Home Theater`, adds **Desktop** and
   **Start-menu** shortcuts, starts the remote-button listener at logon, and
   shows up in **Settings → Apps** for uninstalling.
3. Enter your Jellyfin details: tray icon → **Edit settings** opens
   `config.py` (kept in `%LOCALAPPDATA%\Jellyfin Home Theater`, which updates
   never touch). Save it, then tray icon → **Restart**.

Windows SmartScreen may warn about an unrecognised app the first time — the
exe isn't code-signed. Choose *More info → Run anyway*.

| Shortcut / command | What it does |
|---|---|
| **Jellyfin Home Theater** (Desktop, Start menu) | Starts everything — the app and SignalRGB — or brings the TV screen back if it is already running |
| *Jellyfin Home Theater Remote* (Startup) | Waits at logon for the remote's launch button (`LAUNCH_KEY_VK` / `LAUNCH_POWER_DEVICE` in config.py) |
| `JellyfinHomeTheater.exe --find-displays` | Lists displays, for `MONITOR_NAME` |
| `JellyfinHomeTheater.exe --find-wled` | Finds WLED controllers, for `WLED_IP` |
| `JellyfinHomeTheater.exe --find-ha-lights` | Lists Home Assistant lights, for `HA_LIGHTS` |
| `JellyfinHomeTheater.exe --detect-button` | Shows a remote button's key code, for `LAUNCH_KEY_VK` |

While it runs, the tray icon offers: show the TV screen, the phone remote's
address, updates, edit settings, restart and quit. The logs (`app.log`,
`update.log`, `remote_launcher.log`) sit next to `config.py`.

## Updates

The installed program keeps itself current from this repository's releases:

- **Checks** a minute after it starts and then every 6 hours, downloads a new
  version in the background and verifies its SHA-256.
- **Installs automatically** only when nothing is playing and the TV hasn't
  been touched for 10 minutes (or no TV screen is open), and never in the
  first few minutes after you start it. An open TV page reloads itself onto
  the new version and says *Updated to …*; the kiosk window stays open.
- **Rolls back** if the new version doesn't come up within a minute, and never
  auto-installs that version again.
- **yt-dlp**, the YouTube engine, is kept current separately from yt-dlp's own
  releases, because YouTube breaks old versions every few weeks.

You can also do it by hand, in three places: the TV (side menu → **⚙
Settings**), the phone remote (**⚙ App** tab) and the tray icon — *Check for
updates*, *Install now*, and the *Automatic updates* switch.

---

## Requirements

- **Windows 10/11** for the exe — or **Python 3.10+** to run from source (the
  web app runs anywhere Python does; the kiosk watcher and USB-remote helpers
  use the Windows API)
- A **Jellyfin** server (10.10+ for Skip Intro/Credits; tested on 12.1)
- **Google Chrome** for the kiosk window
- For the YouTube app: **ffmpeg** on `PATH` (`winget install Gyan.FFmpeg`) and
  **Node.js** or **Deno**, which yt-dlp needs to solve YouTube's challenges.
  The exe downloads and updates yt-dlp itself; from source,
  `pip install yt-dlp yt-dlp-ejs`.
- *Optional:* WLED light controllers, Home Assistant, a USB remote

---

## Run from source

### 1. Download and install dependencies

```bash
git clone https://github.com/deniskostadinov1995/jellyfin-home-theater.git
cd jellyfin-home-theater
pip install -r requirements.txt
copy config.example.py config.py
```

### 2. Configure Jellyfin

Edit `config.py` (the installed exe keeps it in `%LOCALAPPDATA%\Jellyfin Home Theater` instead — tray icon → *Edit settings*):

```python
JELLYFIN_URL      = "http://localhost:8096"   # Your Jellyfin server
JELLYFIN_USER     = "Jellyfin"                # Account the TV browses as
JELLYFIN_PASSWORD = "your-password"
PORT              = 5050
```

That's it — no API key or dashboard trip needed. The app logs in with
`/Users/AuthenticateByName` on first run and **caches the access token** in
`.jellyfin_session.json`, so restarting it doesn't open a new Jellyfin session
every time. (Delete that file to force a fresh login.)

Use the account you actually watch with: Continue Watching, Next Up, watched
checkmarks and resume positions all come from that user's data.

> **If login fails with HTTP 403**, the account has hit its *Maximum number of
> simultaneous user sessions* limit. Raise it in **Dashboard → Users → (user) →
> Max simultaneous sessions** (0 = unlimited), or sign an old device out under
> **Dashboard → Devices**.

Tested against **Jellyfin 12.1**. Stream URLs authenticate with the `ApiKey`
query parameter: from 12.x Jellyfin rejects the legacy `api_key` spelling by
default (`EnableLegacyAuthorization` is off), which used to make every
Direct Stream and Transcode answer 401 — only browser-native MP4s still
played.

If Jellyfin isn't up yet when the TV page loads (after a reboot the app can
start first), the home screen says *Waiting for Jellyfin…* and keeps retrying
on its own.

### 3. Run

```bash
python app.py
```

### 4. Open the TV display

On the PC connected to your TV via HDMI:
```
http://localhost:5050/tv
```
Press `F` or click anywhere to go fullscreen.

### 5. Open the Remote on your phone

Find your PC's local IP (run `ipconfig` in cmd), then open on your phone:
```
http://192.168.x.x:5050/remote
```

---

## Remote Controls

### Navigate Tab
- **D-Pad** — move focus (Up/Down/Left/Right)
- **OK** — select/play focused item
- **Back** — go back
- **Home** — return to home screen

**Side menu:** on the home screen, press **Left** from the leftmost column to
pop out a navigation sidebar (Search, Home, YouTube, and each of your
libraries). Press **Right** or **Back** to slide it away. This is the main way
to jump straight into a full library.

### Playback Tab
- Seek bar with current position
- Play/Pause, Stop, ±10s seek
- Next/Previous — for episodes, jumps to the next/previous episode (crossing
  season boundaries); Previous restarts the title first, like a real player

### Volume Tab
- Vertical slider + +/- buttons

---

## TV Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Arrow keys | Navigate |
| Enter | Select (or Skip Intro/Credits when the prompt is up) |
| Space | Play/Pause (in the player) |
| Escape / Backspace | Back |
| F | Toggle fullscreen |
| L | Cycle the ambient lights: Ambilight → Manual → Off |

## YouTube app (ad-free)

The home screen's Apps row has a YouTube tile. It is **not** the YouTube
website — search runs through `yt-dlp` on the backend, and playback streams
the raw media directly into the same player as your library content:

- **No ads, ever** — direct media streams simply don't contain them.
- **SponsorBlock built in** — sponsor/self-promo/intro/outro segments show
  the same Skip button as Jellyfin's intro markers.
- D-pad search with the on-screen keyboard (or type on a real keyboard),
  Recently Watched row, phone remote works exactly as with library content.
- Streams up to **4K (2160p)** — remuxed by ffmpeg (VP9 video + AAC audio)
  into fragmented MP4, no re-encoding, negligible CPU. Lower the ceiling with
  `YT_MAX_HEIGHT` in `config.py` if your PC drops frames at 4K.
- **Self-healing stream** — ffmpeg reconnects automatically if the source
  connection drops mid-video, and a client-side watchdog restarts the stream
  from the current position if playback ever freezes, so a stalled video
  resumes on its own (a manual Play press also forces a reconnect).
- **Seeking is slow, and that's YouTube's doing.** Google serves media only as
  a stream read from the beginning (see below), so jumping to the middle of a
  video means fetching everything up to that point first. Expect a wait
  proportional to how far in you jump — a couple of minutes into a 4K video
  takes tens of seconds. Playing from the start is immediate.

Requirements:

- **yt-dlp** — either the standalone `yt-dlp.exe` on PATH, or the Python
  package (`pip install yt-dlp yt-dlp-ejs`), which the app runs as
  `python -m yt_dlp` when no executable is on PATH.
- **ffmpeg** on PATH.
- **A JavaScript runtime** — Node.js or Deno. Since late 2025 yt-dlp has to
  solve YouTube's JavaScript challenges; without a runtime (and the
  `yt-dlp-ejs` solver scripts, which `yt-dlp.exe` bundles) YouTube refuses
  the streams with 403. The app finds Node.js on PATH and passes it to yt-dlp
  (`--js-runtimes node`), since yt-dlp only looks for Deno by default.

**Keep yt-dlp updated** (`yt-dlp -U`, or `pip install -U yt-dlp yt-dlp-ejs`)
— YouTube changes regularly break old versions. When a video is refused, the
TV says *YouTube refused this video* rather than retrying forever; updating
yt-dlp is the first thing to try. The app logs the yt-dlp version and JS
runtime it found at startup.

Note: the backend runs yt-dlp with the system certificate store
(`--compat-options no-certifi`) so antivirus HTTPS scanning (AVG/Avast…)
doesn't break TLS verification.

If the googlevideo connection drops mid-video (it does when the browser has
buffered far ahead and stopped reading), the fetch proxy carries on from the
exact byte with the next request instead of ending the stream.

### Why there's a fetch proxy in the middle

Nothing can play a googlevideo URL by simply opening it and reading — ffmpeg
included. Google serves media only through its own chunked protocol:

- the byte range goes in the **query string** as `&range=START-END`; a plain
  `Range: bytes=0-` header is answered with **403 Forbidden**;
- every request carries `&rn=N`, a counter starting at 0 and incrementing;
- a stream must be read **from the start, sequentially** — a first request at
  any non-zero offset is refused no matter how `rn` is set, which is why
  seeking costs what it costs;
- one request may cover only about a tenth of the file.

`_YtSourceHandler` in `app.py` speaks that protocol on a small loopback server
and presents ffmpeg an ordinary sequential HTTP stream. If YouTube playback
ever breaks with 403s in the log, that handler is where to look — and updating
yt-dlp is still the first thing to try.

## WLED ambient lights

Find your controllers and paste the line it prints into `WLED_IP`:

```bash
python find_wled.py
```

`WLED_IP` takes a list, so several strips are driven together. Prefer the
mDNS names `find_wled.py` prints (`wled-xxxxxx.local`) over IP addresses: the
strips are on DHCP, and when one moved to a new address it silently dropped
out of every light change. The phone
remote then gains a **💡 Lights** tab, and pressing **L** on the TV cycles the
same three modes with an on-screen confirmation — so the lights can be changed
from wherever you already are, without opening the WLED app or SignalRGB.

### The three modes, and why they exist

SignalRGB creates its ambilight by **streaming** colours to WLED over UDP.
That puts WLED into *realtime* mode, which is what the WLED app is telling you
when it says another device is controlling the lights. It is working as
designed — but while that stream is running, WLED ignores its own on/off and
brightness, so a plain On/Off button appears to do nothing.

WLED has a switch for exactly this, `lor` (live override), and the Lights tab
uses it:

| Mode | What it does | `lor` |
|------|--------------|-------|
| **🎬 Ambilight** | Hands the strips to SignalRGB — they follow the screen | 0 |
| **💡 Manual** | Ignores the stream, so brightness and on/off work again | 2 |
| **🌙 Off** | Ignores the stream *and* switches the strips off — so off stays off | 2 |

You never have to quit SignalRGB to take the lights back; leave it running and
switch modes. Moving the brightness slider implies Manual, so it switches for
you rather than letting the stream immediately overwrite the new value.

The tab also lists each controller with its LED count, marks the one currently
being streamed to as `live`, and lets you toggle a single strip on its own.
Set `WLED_USE_LIVE_OVERRIDE = False` in `config.py` if you would rather the
app never touch the override.

### Dimming when something plays

With `DIM_ON_PLAYBACK = True`, the lights follow what's on screen the way a
cinema does — no button pressing:

| What you're doing | Lights |
|-------------------|--------|
| Browsing | whatever they were before playback started |
| **Playing** | down to `LIGHTS_PLAYING_BRI` (default 40/255), **still running the ambilight** |
| **Paused** | up to `LIGHTS_PAUSED_BRI` (default 120/255) as a steady light, so you can see the room |

Dimming during playback keeps SignalRGB driving the strips, because WLED's
master brightness scales the incoming stream — so the lights still follow the
picture, just at a lower level. Set
`LIGHTS_AMBILIGHT_WHILE_PLAYING = False` to hold them at a plain dim glow
instead, or `LIGHTS_PLAYING_BRI = 0` to switch them off outright.

Two details that make it liveable:

- **It only acts when the state changes.** Grab the remote mid-film and set
  whatever you like — nothing will argue with you until you next pause or stop.
- **It restores what was actually there.** The brightness in use before
  playback is remembered and put back afterwards, rather than a guess. Pin a
  fixed level with `LIGHTS_IDLE_BRI` if you prefer.

It works for YouTube as well as library content, and a **Dim when playing**
switch on the Lights tab turns it off for the evening without editing the
config. If the TV page goes away mid-film, a watchdog brings the lights back up
after 90 seconds rather than leaving the room dark.

### Ceiling lights, through Home Assistant

The same automation can dim lights that aren't WLED — the living-room ceiling,
for instance. Set `HA_URL` and a long-lived access token in `config.py` (Home
Assistant → your user, bottom left → Security → *Long-lived access tokens* →
Create token), then:

```bash
python find_ha_lights.py
```

It lists your `light.*` entities with their current levels and prints the
`HA_LIGHTS = [...]` line to paste back.

The ceiling has its own levels, because it wants to go much further down than
the ambilight does:

| | Ceiling (`HA_*_PCT`) | WLED strips (`LIGHTS_*_BRI`) |
|---|---|---|
| Playing | 15% | 40/255, ambilight still running |
| Paused | 50% | 120/255 |
| Browsing | back to what it was | back to what it was |

Set `HA_PLAYING_PCT = 0` if you would rather the ceiling go out completely.

Each set of lights is judged separately: anything that was already **off** when
playback started is left off, so switching the ceiling off by hand and then
starting a film won't turn it back on at a dim setting. Both appear on the
remote's Lights tab with their own on/off, and the **Off** mode (and the **L**
key) takes everything out together.

## Screen aspect / removing black bars (for ambient lighting)

Cinematic titles are letterboxed (black bars top & bottom) and old 4:3 shows
are pillarboxed (bars at the sides). Those bars are **dead zones** for
SignalRGB's screen-capture ambient lighting — the edge LEDs just show black.

The player has a **🖥 aspect button** (also `Z` on the keyboard, and a
**🖥 Screen Fit** button on the phone remote's Playback tab) that cycles:

| Mode | What it does | Ambient lighting |
|------|--------------|------------------|
| **Auto** (default) | Detects the content's real shape and zooms to fill, removing bars automatically | Edges always lit ✓ |
| **Fit** | Whole image, bars preserved | Top/bottom LEDs dark |
| **Fill** | Zoom to fill, crop overflow | Edges always lit ✓ |
| **Zoom** | Extra overscan (for bars baked into the file) | Edges always lit ✓ |

**Auto** works for every source: it compares the video's true aspect ratio to
the screen and fills when they differ (covers the common case where bars are
added by the display), and additionally samples the frame to catch bars baked
into the file. Your choice is remembered between sessions.

The trade-off: Fill/Auto crop a sliver of the picture to banish the bars — a
2.40:1 film loses a little of its left/right edges. Purists can leave it on
**Fit** and accept dark top/bottom LEDs. This is handled in the player (not by
poking SignalRGB), so it works reliably and needs no SignalRGB configuration.

## USB remote — making it always control the TV

A USB remote is just an HID keyboard, so Windows sends its arrow keys to
whichever window is in front. If you are using the PC for something else, the
remote drives *that* instead of Jellyfin — and the only fix was to walk over
and click the kiosk window.

`remote_bridge.py` solves it. Run the probe once to find out which device your
remote is:

```bash
python remote_probe.py
```

Press every button on the remote, then the same keys on your keyboard, then
Ctrl+C. It prints which physical device each keypress came from and the
`REMOTE_DEVICE = "..."` line to paste into `config.py`. Then:

```bash
python remote_bridge.py
python remote_bridge.py --install    # start at logon (undo: --uninstall)
```

### How it works

Three pieces, because no single one of them is enough:

1. **Raw Input**, registered with `RIDEV_INPUTSINK`, reports *which physical
   device* each keystroke came from, and arrives even though the bridge has no
   focused window. That is what tells the remote apart from your keyboard.
2. A **low-level keyboard hook** swallows the remote's keys so they never reach
   the app you are using. Raw Input cannot block a keystroke; the hook can, but
   the hook is not told which device is responsible — hence needing both.
3. The keypress is forwarded to the TV as a command over HTTP
   (`POST /api/command`), the same channel the phone remote uses. **The kiosk
   window never needs focus at all.**

Your keyboard is never touched: only keystrokes from the configured device are
intercepted. So you can keep working on the PC while the remote drives the TV.

### Staying out of the way

- **No TV page open → keys pass straight through**, so the remote behaves like
  an ordinary keyboard when you are not watching anything. Presence is tracked
  by the TV's socket connection, not a heartbeat, because the TV goes quiet
  whenever nothing on screen is changing.
- **Unmapped buttons pass through** untouched.
- Set `REMOTE_TOGGLE_KEY` to one of the remote's buttons and it turns bridging
  off and on, for when you want the remote to act as a plain keyboard.
- `REMOTE_KEYS` in `config.py` remaps any button; set an action to `None` to
  leave that key to Windows.

Both key-down and key-up are swallowed for intercepted keys — passing a release
without its press makes some applications think a key is stuck.

## USB remote launcher

> The installed exe sets all of this up for you: the *Jellyfin Home Theater Remote* Startup shortcut runs `JellyfinHomeTheater.exe --listener`. The launch button is configured in `config.py` — `LAUNCH_KEY_VK`, `LAUNCH_KEY_MODS`, `LAUNCH_POWER_DEVICE`, `SIGNALRGB_EXE` — and `JellyfinHomeTheater.exe --detect-button` finds the key code. The rest of this section describes running it from source.

`remote_launcher.py` binds one button on a USB remote to "turn everything on":
when pressed, it starts **Jellyfin Home Theater** and **SignalRGB** — each only
if it isn't already running. No dependencies — it uses the built-in Windows API
via ctypes (same as `monitor_watcher.py`). Setup (one time):

```bash
python remote_launcher.py --detect      # press your button, copy the printed HOTKEY_VK into the config
python remote_launcher.py               # test it
python remote_launcher.py --install     # auto-start at logon (undo: --uninstall)
```

It finds SignalRGB automatically (via its update-proof launcher stub), starts
the app with `pythonw` (no console window), and logs activity to
`remote_launcher.log`. Full instructions are in the script's header comment.

`python remote_launcher.py --once` runs the launch action once and exits — the
**Jellyfin Home Theater** desktop shortcut uses exactly that.

If the app is already running, the button asks it to show the TV UI
(`POST /api/kiosk/show`, accepted from this PC only): a kiosk window that was
closed is reopened, and one that is open is brought to the front. It used to
do nothing in that case, so a kiosk closed with Alt+F4 stayed closed.

The app itself runs windowless under `pythonw`, so it logs to **`app.log`**
next to `app.py` (rotated at 2 MB) — startup status, warnings, and the
traceback of anything that crashes.

### The remote's on/off button

The on/off button is **not a keyboard key**, so `--detect` can't see it. It
sends a HID *System Control* **Sleep** on the remote's separate "HID-compliant
system controller" interface, which Windows treats as the PC's sleep button —
that's why pressing it used to put the PC to sleep (event log: *Sleep Reason:
Button or Lid*).

The launcher reads that interface directly (`SYSTEM_KEY_DEVICE`, default
`VID_248A&PID_168E`) and fires the same launch action on Sleep/Power. Raw Input
can't be used for this — Windows never lists System Control collections there —
but the collection can be opened for shared reading. Reports are decoded with
the device's own descriptor: this remote uses report ID 4 with a selector byte
(`01` Sleep, `02` Power, `03` Wake, `00` released), not one bit per key.

**One Windows setting is required**, or the PC launches the app *and* sleeps:
Control Panel → Power Options → *Choose what the power buttons do* →
**When I press the sleep button: Do nothing**. This doesn't touch the PC's own
power button, and Start → Sleep still works.

## Playback features

- **Three-tier playback** — the TV picks the cheapest path that works:

  | Tier | What Jellyfin does | When |
  |------|--------------------|------|
  | **Direct Play** | Serves the original file untouched | Browser-friendly container *and* audio (MP4/H.264/AAC…) |
  | **Direct Stream** | Remuxes the container and converts only the audio; **video is copied, never re-encoded** | MKV containers, or DTS/TrueHD/E-AC3 audio a browser can't decode |
  | **Transcode** | Re-encodes the video too | Codec the browser can't decode at all, or you picked a specific resolution |

  The on-screen badge tells you which one you're on, and the method is
  reported back to Jellyfin so its dashboard matches.

- **Skip Intro / Skip Credits** — driven by Jellyfin's **media segments**
  (`/MediaSegments`), which is built in as of Jellyfin 10.10 — no Intro Skipper
  plugin needed. Intro, Outro, Recap, Preview and Commercial segments each get
  their own Skip button; press OK/Enter (or click) to skip. A segment that runs
  to the end of the file jumps straight to Up Next.

- **Instant subtitles on Direct Play** — Jellyfin converts text subtitles
  (SRT/ASS, external or embedded) to WebVTT itself, so selecting one downloads
  it on the spot and renders it over the video without interrupting playback.
  Only image-based subtitles (PGS/VobSub) fall back to a burn-in transcode.
  The selected subtitle carries over automatically to reloads and Up Next
  episodes.

  One deliberate difference from Jellyfin's own clients: when a file's *default*
  subtitle is image-based, it starts **off** rather than silently forcing a
  burn-in transcode the moment you press Play. Pick it from the subtitle menu
  to turn it on.

- **Multiple versions** — when a title exists as more than one file (say a
  1080p rip next to a 4K remux), the highest-quality one plays by default and
  the player's **Quality** menu grows a **Version** section to switch between
  them.

- **Quality defaults to Maximum (original)** — Direct Play whenever the
  browser can handle the file; picking any specific resolution forces a
  transcode at that resolution.

- **Auto "Up Next"** — a 10-second countdown queues the next episode.

- **Stream error recovery** — network/decode hiccups are retried in place
  instead of killing playback.

---

## Architecture

```
Phone Remote ──WebSocket──► Flask + SocketIO ──WebSocket──► TV Display
                                    │
                                    └──HTTP──► Jellyfin Server :8096
```

- **`homeassistant.py`** — a small Home Assistant REST client, used only by
  the light automation. Everything crossing its boundary is 0–255 brightness,
  matching WLED; percentages exist only in `config.py` and at the service call.
- **`jellyfin.py`** — the Jellyfin client plus the adapter that flattens
  Jellyfin's item schema into the shape the TV frontend consumes. All Jellyfin
  knowledge lives here; `static/js/tv.js` never sees a Jellyfin field name.
- **Image proxy** at `/img` — fetches artwork server-side, so the token never
  reaches the page HTML (and the path is validated, so it can only ever fetch
  item artwork).
- **Stream URLs** at `/api/stream_url/<id>` — returns the three playback URLs,
  track lists, media segments and available versions in one call.
- **Progress reporting** at `/api/timeline` — drives `/Sessions/Playing*`, so
  Jellyfin's dashboard shows the session and resume positions sync with your
  other clients.

### Why an adapter instead of a rewrite

The TV UI was originally built against Plex's schema. Rather than rewrite
2,600 lines of navigation and player logic, `jellyfin.py` translates Jellyfin's
responses into the same flat item shape (`ratingKey`, `viewOffset`,
`grandparentTitle`, …). If you ever need to read the frontend, note that those
names are historical: `ratingKey` is a Jellyfin item id, `viewOffset` is
`UserData.PlaybackPositionTicks` in milliseconds, and so on.

One wrinkle worth knowing about: audio and subtitle **track ids are the
Jellyfin stream index + 1**. Jellyfin's stream indexes start at 0 and the
frontend treats a falsy track id as "Off", so without the offset a subtitle at
index 0 would read as "no subtitles".

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Home screen says "Waiting for Jellyfin…" | It retries by itself until the server answers. If it never does, check `JELLYFIN_URL`, `JELLYFIN_USER` and `JELLYFIN_PASSWORD` in config.py — `app.log` has the exact reason. |
| Only some MP4s play; MKVs fail with "Stream error" | The Jellyfin server rejected the stream's credentials. From Jellyfin 12 the legacy `api_key` URL parameter is refused; the app uses `ApiKey`. If a future server changes this again, the HLS request in Chrome's network tab will show 401. |
| Where are the logs? | `app.log` next to `app.py` (the launcher starts the app windowless, so there is no console). `remote_launcher.log` and `remote_bridge.log` cover the helpers. |
| Login fails with HTTP 403 | The account's simultaneous-session limit is full. Raise it in Dashboard → Users, or sign out an old device in Dashboard → Devices. |
| Login fails with HTTP 401 | Wrong username or password. Delete `.jellyfin_session.json` after fixing config.py. |
| Video won't play | Check the badge: on Transcode, make sure Jellyfin's ffmpeg is configured (Dashboard → Playback). Try another version from the Quality menu if the title has several. |
| Playback stutters / not smooth | The kiosk launches Chrome with GPU + no-throttle flags and tunes the HLS buffer for local streams. If it still stutters, confirm Chrome shows hardware decode at `chrome://gpu`, and that the on-screen badge says Direct Play / Direct Stream rather than Transcoding. |
| Audio is stereo when you want 5.1 | Direct Play keeps the original track untouched. Anything else converts audio to AAC capped at `MAX_AUDIO_CHANNELS` in config.py — raise it to `6`. |
| Picture drops to a tiny resolution seconds after starting | Jellyfin sizes a stream to fit the bitrate it is given, and assumes 256 kbps when given none — which scales a 4K film to 416x177. `MAX_STREAMING_BITRATE` in config.py is that budget; it should stay high on a LAN. |
| Badge says Direct Stream but the CPU is busy transcoding | Jellyfin's copy playlist also offers a tone-mapped SDR rung and an H.264 fallback rung, and advertises all of them at the same bandwidth, so hls.js will happily pick a re-encode. The player pins the genuine copy variant by codec and dynamic range (`pickCopyLevel` in tv.js); the console logs which one it chose. |
| YouTube video won't start, 403s in the log, or "YouTube refused this video" | Update yt-dlp (`pip install -U yt-dlp yt-dlp-ejs`) and make sure Node.js or Deno is installed — see *YouTube app*. Then *Why there's a fetch proxy in the middle* above. |
| `pip install` fails with `RecursionError` in `ssl` | Something (often a `usercustomize.py` added to work around antivirus HTTPS scanning) injects truststore into every Python process, and pip injects it a second time. Run pip as `pip --use-deprecated=legacy-certs install …`, which skips pip's own copy. |
| No Skip Intro button | Jellyfin needs media segments for that item. Run an intro-detection task/plugin, or check `Dashboard → Scheduled Tasks`. Titles without segments simply never show the button. |
| Kiosk launches on the wrong TV input, or not at all | Run `python monitor_watcher.py` to see which DDC/CI registers your TV answers. Set `EXPECTED_INPUT_VCP` to the value it reports while on the PC's input. If it answers nothing, set `REQUIRE_ACTIVE_INPUT = False`. |
| The whole UI freezes for several seconds | Fixed: Flask-SocketIO used to pick gevent automatically (because gevent is installed) without the monkeypatching gevent requires, so every blocking call stalled the entire server — a screen of posters serialised into a ~16-second freeze that also froze the remote. `async_mode` is now pinned to `"threading"` in app.py; don't change it back without monkeypatching first. |
| D-pad moves to the wrong card | Fixed: the focus grid is derived from the rendered layout rather than a fixed column count, and rebuilds itself when the window changes size (including the fullscreen toggle). |
| Lights won't turn off | SignalRGB is streaming to them; use the Lights tab's **Off** mode (or press **L** on the TV), which tells WLED to ignore the stream. See *WLED ambient lights*. |
| Lights stay dim after watching | They restore on pause/stop; if the TV page was closed mid-playback instead, the watchdog brings them up within 90s. Pressing **L** or using the Lights tab fixes it immediately. |
| Don't want the auto-dimming | Turn off **Dim when playing** on the Lights tab, or set `DIM_ON_PLAYBACK = False`. |
| Lights tab says no controller configured | Run `python find_wled.py` and put the result in `WLED_IP`. |
| One strip shows "unreachable" | Its DHCP address changed. Use the `.local` names `find_wled.py` prints, which follow the device. |
| Remote's power button opens nothing while the app is running | The kiosk only opens while the TV is the active display — check `MONITOR_NAME` (`python find_displays.py`, with the TV on). `app.log` records each "TV UI requested" and what the watcher decided. |
| Ceiling lights don't respond | Check `HA_TOKEN` is set and `HA_LIGHTS` names real entities — `python find_ha_lights.py` lists them. A bad token shows up as HTTP 401 in the log. |
| Remote controls the wrong window | That is what `remote_bridge.py` is for — see *USB remote*. Check `REMOTE_DEVICE` is set and the bridge is running; `remote_bridge.log` records what it forwarded. |
| Remote does nothing at all | With `REMOTE_ONLY_WHEN_TV_RUNNING = True` the bridge deliberately stands aside when no TV page is open. Open `/tv`, or set it `False`. |
| Phone can't reach the app | Make sure PC firewall allows port 5050 |
| Remote not connecting | Both phone and PC must be on the same Wi-Fi |

### How "is the TV on my input?" is detected

The watcher no longer relies on a single DDC/CI read. It uses a layered model
(see `config.py`): it first asks the TV which input it is showing (VCP `0x60`,
retried over a reused connection), falls back to the TV's power state (VCP
`0xD6`) if the TV won't answer that, and finally falls back to plain display
presence if the TV answers no DDC/CI at all. It only starts the TV UI when the
strongest available signal says the TV is really on the PC's HDMI input, and
otherwise keeps quietly monitoring until it is.

One subtlety: many TVs only answer DDC/CI on the input they are currently
showing. So once a TV has *proven* it answers input-source reads, sustained
silence is treated as "switched to another input / turned off" (kiosk closes)
rather than "DDC/CI unsupported" — silence from a talkative TV is itself a
signal.

---

## Building a release

```bash
pip install -r requirements.txt pyinstaller pystray pillow psutil truststore
python build.py      # dist\JellyfinHomeTheater\  +  release\JellyfinHomeTheater-<version>-win64.zip  +  release\latest.json
python release.py    # publishes both files as GitHub release v<version>
```

Bump `__version__` in `version.py` and add a section to `CHANGELOG.md` first:
that section becomes the release notes and what the update prompt shows.
`build.py` refuses to produce a build that contains `config.py` or your
Jellyfin password, so a public release can never carry your settings.
Installed copies pick the new release up on their next check.

---

## License

[MIT](LICENSE) © Denis Kostadinov
