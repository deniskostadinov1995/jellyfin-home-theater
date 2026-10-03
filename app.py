"""
Jellyfin Home Theater - Flask Backend
Serves the TV display page and Phone Remote, relays WebSocket commands between them.

All Jellyfin traffic goes through jellyfin.py, which also translates Jellyfin's
item schema into the flat shape the TV frontend consumes.  Routes here keep the
names and response shapes the frontend already expects.
"""

import safe_console
safe_console.enable()   # arrows/emoji in output must not crash cp1252 consoles

from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO, emit
import requests
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, parse_qsl

import jellyfin
from jellyfin import JellyfinClient, JellyfinError, ms_to_ticks
from homeassistant import HomeAssistantClient, HomeAssistantError, pct_to_255

# ── Load config ──────────────────────────────────────────────────────────────
try:
    from config import (
        JELLYFIN_URL, JELLYFIN_USER, JELLYFIN_PASSWORD, SECRET_KEY, PORT,
        MONITOR_NAME, CHROME_PATH, MONITOR_POLL_INTERVAL,
    )
except ImportError:
    print("⚠  config.py not found or missing Jellyfin settings.")
    print("   It needs JELLYFIN_URL, JELLYFIN_USER and JELLYFIN_PASSWORD.")
    sys.exit(1)

# Optional: HDMI-input detection settings (DDC/CI).  Safe defaults if absent.
try:
    from config import REQUIRE_ACTIVE_INPUT
except ImportError:
    REQUIRE_ACTIVE_INPUT = False
try:
    from config import EXPECTED_INPUT_VCP
except ImportError:
    EXPECTED_INPUT_VCP = None
try:
    from config import FALLBACK_TO_POWER_STATE
except ImportError:
    FALLBACK_TO_POWER_STATE = True
try:
    from config import MAX_AUDIO_CHANNELS
except ImportError:
    MAX_AUDIO_CHANNELS = 2
try:
    from config import MAX_STREAMING_BITRATE
except ImportError:
    MAX_STREAMING_BITRATE = 120_000_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jht")

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

# async_mode is pinned deliberately — do not remove it.
#
# Left to itself, Flask-SocketIO picks "gevent" whenever gevent happens to be
# installed, and gevent is only correct if the standard library has been
# monkeypatched first. This app never does that, so every request ran on one
# cooperative hub where a single blocking call stalled the whole server: a
# screen of posters serialised into a ~16-second freeze that took the phone
# remote down with it.
#
# Monkeypatching is not the answer here either. monitor_watcher makes blocking
# ctypes DDC/CI calls that can take seconds, and under a patched runtime those
# would freeze the hub exactly the same way.
#
# "threading" gives each request a real OS thread, which is what this workload
# wants: a couple of clients doing blocking I/O, plus ffmpeg/yt-dlp
# subprocesses. It still serves real WebSockets through simple-websocket — it
# just will not accept one as the opening transport, which is why both clients
# start on polling and upgrade (see the note beside `io(...)` in tv.js).
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

DEVICE_ID = "jellyfin-home-theater"

# The access token is cached here so restarting the app reuses its Jellyfin
# session instead of opening a new one each time — accounts can have a
# "maximum simultaneous sessions" limit that would otherwise fill up.
# Delete this file to force a fresh login.
TOKEN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".jellyfin_session.json")

jf = JellyfinClient(JELLYFIN_URL, JELLYFIN_USER, JELLYFIN_PASSWORD,
                    device_id=DEVICE_ID, token_cache=TOKEN_CACHE)

# ── Global playback state (shared between TV and Remote) ─────────────────────
state = {
    "screen":    "home",   # home | library | detail | player
    "playing":   False,
    "title":     "",
    "subtitle":  "",
    "thumb":     "",
    "position":  0,
    "duration":  0,
    "volume":    100,
    "item_key":  None,
}

# Which file (media source) each item is currently playing, plus the play
# session Jellyfin handed us.  Subtitle fetches and session reporting both
# need to refer to the exact source the player picked — an item can have
# several versions with different stream layouts.
_active_source = {}

# Audio / subtitle picks, keyed by media-source id.  Jellyfin has no
# server-side "select this track" call the way Plex did (track choice is a
# stream-URL parameter), so the choice is remembered here and applied every
# time a stream URL is built for that file.
_stream_choices = {}

# Items we have already announced to Jellyfin as "now playing", so progress
# reports go to the right endpoint.
_playing_now = set()

PLAY_METHODS = {
    "direct":       "DirectPlay",
    "directStream": "DirectStream",
    "transcode":    "Transcode",
}


# Upstream error text embeds the failing request URL, which carries the access
# token and user id. Those must not reach a browser response or the log.
_SECRET_QUERY_RE = re.compile(
    r"(api_key|apikey|X-Emby-Token|X-MediaBrowser-Token|userId|token)"
    r"=[^&\s\"']+",
    re.IGNORECASE)


def safe_error(exc):
    """A message fit to return to a client: no credentials, no noise."""
    return _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=***", str(exc))


def _remember(store, key, value, cap=200):
    """Tiny bounded memo — a home theater never holds many of these, but an
    unbounded dict in a long-running server is still a leak."""
    if len(store) > cap:
        store.clear()
    store[key] = value


# ── Library views ────────────────────────────────────────────────────────────
_views = {"ts": 0.0, "items": []}
_VIEWS_TTL = 300


def get_views(force=False):
    if force or time.time() - _views["ts"] > _VIEWS_TTL:
        data = jf.get_json("/UserViews", jf.user_params()) or {}
        _views["items"] = data.get("Items") or []
        _views["ts"] = time.time()
    return _views["items"]


def view_collection_type(view_id):
    for v in get_views():
        if v.get("Id") == view_id:
            return (v.get("CollectionType") or "").lower()
    # Cache may predate a newly added library — refresh once before giving up.
    for v in get_views(force=True):
        if v.get("Id") == view_id:
            return (v.get("CollectionType") or "").lower()
    return ""


# ── Page routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/tv")
def tv():
    return render_template("tv.html")

@app.route("/remote")
def remote():
    return render_template("remote.html")

# ── Jellyfin API proxy (keeps the token server-side, avoids CORS) ─────────────
@app.route("/api/libraries")
def api_libraries():
    """Real media libraries for the sidebar and home rows.

    Jellyfin exposes Collections and Playlists as user views too, but the home
    screen already has its own rows for those — listing them as libraries
    would show everything twice.
    """
    try:
        libs = [jellyfin.to_library(v) for v in get_views()]
        libs = [l for l in libs if l["collectionType"] not in ("boxsets", "playlists")]
        return jsonify(libs)
    except Exception as e:
        log.warning("libraries failed: %s", e)
        return jsonify({"error": safe_error(e)}), 502


def _library_query(section_id):
    """Base /Items query for browsing one library."""
    ctype = view_collection_type(section_id)
    params = {
        "parentId":  section_id,
        "recursive": "true",
        "fields":    jellyfin.LIST_FIELDS,
    }
    types = jellyfin.VIEW_ITEM_TYPES.get(ctype)
    if types:
        params["includeItemTypes"] = types
    return params


def _apply_filters(params, args):
    """Genre / decade / unwatched filters, as the TV toolbar sends them."""
    genre     = args.get("genre")
    decade    = args.get("decade")
    unwatched = args.get("unwatched")

    if genre and genre != "all":
        params["genres"] = genre
    if decade and decade not in ("all", ""):
        try:
            start = int(decade)
            # Jellyfin filters on an explicit year list, not a decade tag.
            params["years"] = ",".join(str(y) for y in range(start, start + 10))
        except ValueError:
            pass
    if unwatched == "1":
        params["isPlayed"] = "false"
    return params


@app.route("/api/library/<section_id>/all")
def api_library_all(section_id):
    """
    List items in a library section.

    Supports query params:
      start, size       – pagination
      sort              – titleSort | addedAt:desc | originallyAvailableAt:desc | year:desc | rating:desc
      genre             – Jellyfin genre name (e.g. "Action")
      decade            – e.g. 1990 (expanded to the years 1990–1999)
      unwatched         – "1" to show only unwatched
    """
    try:
        sort_by, sort_order = jellyfin.map_sort(request.args.get("sort"))
        params = _library_query(section_id)
        params.update({
            "startIndex": request.args.get("start", 0),
            "limit":      request.args.get("size", 50),
            "sortBy":     sort_by,
            "sortOrder":  sort_order,
        })
        _apply_filters(params, request.args)
        items, _ = jf.items(params)
        return jsonify(jellyfin.to_items(items))
    except Exception as e:
        log.warning("library %s listing failed: %s", section_id, e)
        return jsonify({"error": safe_error(e)}), 502


@app.route("/api/library/<section_id>/count")
def api_library_count(section_id):
    """Returns the total number of items so the frontend can paginate."""
    try:
        params = _library_query(section_id)
        params["limit"] = 1
        _apply_filters(params, request.args)
        _, total = jf.items(params)
        return jsonify({"total": total})
    except Exception as e:
        log.warning("library %s count failed: %s", section_id, e)
        return jsonify({"total": 0})


@app.route("/api/library/<section_id>/genres")
def api_library_genres(section_id):
    """List genres present in a section (for the filter dropdown)."""
    try:
        data = jf.get_json("/Genres", jf.user_params(
            {"parentId": section_id, "sortBy": "SortName"})) or {}
        return jsonify([{"title": g.get("Name"), "key": g.get("Name")}
                        for g in (data.get("Items") or []) if g.get("Name")])
    except Exception:
        return jsonify([])


@app.route("/api/library/<section_id>/decades")
def api_library_decades(section_id):
    """Decades present in a section, newest first.

    Jellyfin has no decade concept — it reports the individual years, which
    get folded down to decades here.
    """
    try:
        data = jf.get_json("/Years", jf.user_params(
            {"parentId": section_id, "sortBy": "ProductionYear"})) or {}
        years = []
        for y in data.get("Items") or []:
            name = str(y.get("Name") or "")
            if name.isdigit():
                years.append(int(name))
        decades = sorted({(y // 10) * 10 for y in years}, reverse=True)
        return jsonify([{"title": str(d), "key": str(d)} for d in decades])
    except Exception:
        return jsonify([])


@app.route("/api/on_deck")
def api_on_deck():
    """Continue Watching: partly-watched titles first, then the next unwatched
    episode of each show in progress."""
    out = []
    seen = set()
    try:
        resume = jf.get_json("/UserItems/Resume", jf.user_params(
            {"limit": 20, "mediaTypes": "Video",
             "fields": jellyfin.LIST_FIELDS})) or {}
        for j in resume.get("Items") or []:
            if j.get("Id") not in seen:
                seen.add(j.get("Id"))
                out.append(j)
    except Exception as e:
        log.warning("resume list failed: %s", e)

    try:
        nextup = jf.get_json("/Shows/NextUp", jf.user_params(
            {"limit": 20, "fields": jellyfin.LIST_FIELDS})) or {}
        for j in nextup.get("Items") or []:
            if j.get("Id") not in seen:
                seen.add(j.get("Id"))
                out.append(j)
    except Exception as e:
        log.warning("next-up list failed: %s", e)

    return jsonify(jellyfin.to_items(out))


@app.route("/api/recently_added")
def api_recently_added():
    """Newest additions. /Items/Latest groups new episodes under their show,
    which is what a "recently added" row should show."""
    try:
        data = jf.get_json("/Items/Latest", jf.user_params(
            {"limit": 30, "fields": jellyfin.LIST_FIELDS}))
        # This endpoint answers with a bare array, not an { Items: [] } object.
        items = data if isinstance(data, list) else (data or {}).get("Items") or []
        return jsonify(jellyfin.to_items(items))
    except Exception as e:
        log.warning("recently added failed: %s", e)
        return jsonify([])


@app.route("/api/metadata/<rating_key>")
def api_metadata(rating_key):
    """Full metadata including cast, crew, genres and ratings."""
    try:
        j = jf.item(rating_key, {"fields": jellyfin.DETAIL_FIELDS})
        return jsonify(jellyfin.to_detail(j))
    except Exception as e:
        log.warning("metadata %s failed: %s", rating_key, e)
        return jsonify({"error": safe_error(e)}), 502


@app.route("/api/metadata/<rating_key>/children")
def api_children(rating_key):
    """Seasons of a show, or episodes of a season.

    Jellyfin's natural child order is already index order, but sorting
    explicitly keeps specials and out-of-order numbering predictable.
    """
    try:
        items, _ = jf.items({"parentId": rating_key,
                             "fields": jellyfin.LIST_FIELDS})
        items.sort(key=lambda j: (j.get("ParentIndexNumber") or 0,
                                  j.get("IndexNumber") or 0))
        return jsonify(jellyfin.to_items(items))
    except Exception as e:
        log.warning("children of %s failed: %s", rating_key, e)
        return jsonify([])


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        items, _ = jf.items({
            "searchTerm":       q,
            "recursive":        "true",
            "includeItemTypes": "Movie,Series,Episode,BoxSet",
            "limit":            50,
            "fields":           jellyfin.LIST_FIELDS,
        })
        return jsonify(jellyfin.to_items(items))
    except Exception as e:
        log.warning("search failed: %s", e)
        return jsonify([])


# ── Collections & Playlists ────────────────────────────────────────────────
@app.route("/api/collections")
def api_collections():
    try:
        items, _ = jf.items({
            "recursive":        "true",
            "includeItemTypes": "BoxSet",
            "sortBy":           "SortName",
            "limit":            200,
            "fields":           jellyfin.LIST_FIELDS,
        })
        return jsonify(jellyfin.to_items(items))
    except Exception as e:
        log.warning("collections failed: %s", e)
        return jsonify([])


@app.route("/api/collection/<rating_key>/children")
def api_collection_children(rating_key):
    try:
        items, _ = jf.items({"parentId": rating_key,
                             "sortBy": "SortName",
                             "fields": jellyfin.LIST_FIELDS})
        return jsonify(jellyfin.to_items(items))
    except Exception:
        return jsonify([])


@app.route("/api/playlists")
def api_playlists():
    """List video playlists (audio playlists aren't browsable here yet)."""
    try:
        items, _ = jf.items({
            "recursive":        "true",
            "includeItemTypes": "Playlist",
            "sortBy":           "SortName",
            "fields":           jellyfin.LIST_FIELDS,
        })
        items = [j for j in items if (j.get("MediaType") or "Video") != "Audio"]
        return jsonify(jellyfin.to_items(items))
    except Exception:
        return jsonify([])


@app.route("/api/playlist/<rating_key>/items")
def api_playlist_items(rating_key):
    try:
        data = jf.get_json(f"/Playlists/{rating_key}/Items", jf.user_params(
            {"fields": jellyfin.LIST_FIELDS})) or {}
        return jsonify(jellyfin.to_items(data.get("Items")))
    except Exception:
        return jsonify([])


# ── Random art (for screensaver) ───────────────────────────────────────────
@app.route("/api/random_art")
def api_random_art():
    """Random backdrops for the screensaver to cycle through."""
    try:
        n = min(int(request.args.get("n", 30)), 100)
    except ValueError:
        n = 30
    try:
        items, _ = jf.items({
            "recursive":        "true",
            "includeItemTypes": "Movie,Series",
            "sortBy":           "Random",
            "imageTypes":       "Backdrop",
            "limit":            n,
        })
        out = []
        for j in items:
            art = (j.get("BackdropImageTags") or [None])[0]
            if not art:
                continue
            out.append({
                "art":   jellyfin.image_path(j["Id"], "Backdrop", 0, tag=art),
                "title": j.get("Name") or "",
                "year":  j.get("ProductionYear") or "",
            })
        return jsonify(out)
    except Exception as e:
        log.warning("random art failed: %s", e)
        return jsonify([])


# Image proxy — streams Jellyfin artwork without putting the token in page HTML
@app.route("/img")
def img_proxy():
    img_path = request.args.get("path", "")
    base, _, query = img_path.partition("?")
    if not jellyfin.is_safe_image_path(base):
        # Only item artwork goes through here — never an arbitrary server path.
        return "", 404

    params = dict(parse_qsl(query))
    params.update({
        "fillWidth":  request.args.get("w", 400),
        "fillHeight": request.args.get("h", 600),
        "quality":    90,
    })

    try:
        r = jf.request("GET", base, params=params, stream=True, timeout=15)
    except requests.HTTPError as exc:
        # Forward failures as failures — a 200 with garbage bytes leaves the
        # <img> permanently broken; a real error status lets the TV fall back.
        # The streamed response has to be closed by hand: raise_for_status()
        # leaves its connection open, and a library with missing artwork asks
        # for the same broken poster on every page load.
        if exc.response is None:
            return "", 502
        status = exc.response.status_code
        exc.response.close()
        return "", status
    except Exception:
        return "", 502

    return Response(
        r.iter_content(chunk_size=8192),
        content_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={
            # Artwork for a given tag+size never changes — let Chrome cache it
            # so the home screen doesn't refetch ~100 images per load.
            "Cache-Control": "public, max-age=604800, immutable",
        },
    )


# ── Stream URL builder — returns URLs + full quality metadata ────────────────
def _parse_resolution(value):
    """"1920x1080" → (1920, 1080); "original" / junk → (None, None)."""
    if not value or value == "original":
        return None, None
    try:
        w, _, h = value.partition("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return None, None


@app.route("/api/stream_url/<rating_key>")
def stream_url(rating_key):
    """
    Build the three-tier playback URLs.

    Query params:
      resolution    – e.g. "1920x1080", "1280x720", "3840x2160", or "original"
      max_bitrate   – kbps cap for the transcoder. Optional.
      media_source  – play a specific version of the title. Optional; the
                      highest-quality one is chosen by default.
    """
    try:
        target_resolution = request.args.get("resolution", "original")
        max_bitrate       = request.args.get("max_bitrate")
        wanted_source     = request.args.get("media_source")

        try:
            pbi = jf.get_json(f"/Items/{rating_key}/PlaybackInfo",
                              jf.user_params()) or {}
        except Exception:
            # Folder items — a series, a season, a collection — have no media
            # of their own, and Jellyfin answers PlaybackInfo for them with a
            # 500 rather than something descriptive.
            return jsonify({"error": "item has no playable media"}), 404
        sources = pbi.get("MediaSources") or []
        source  = jellyfin.pick_source(sources, wanted_source)
        if not source:
            return jsonify({"error": "no playable media source"}), 404

        msid    = source.get("Id") or rating_key
        session = pbi.get("PlaySessionId")

        # Switching quality, version or audio track re-enters this route and
        # gets a fresh play session, orphaning the previous transcode. Stop it
        # rather than leaving an ffmpeg running for nothing.
        previous = _active_source.get(rating_key) or {}
        if previous.get("playSessionId") and previous["playSessionId"] != session:
            _stop_transcode(previous["playSessionId"])

        # Remember which file is playing — subtitle fetches and session
        # reporting both need the exact source, not just the item.
        _remember(_active_source, rating_key,
                  {"msid": msid, "playSessionId": session})

        # Apply any track the user picked earlier for this file.  A stored
        # subtitle index of -1 means they explicitly chose "Off".
        choice    = _stream_choices.get(msid, {})
        audio_idx = choice.get("audio", source.get("DefaultAudioStreamIndex"))
        sub_idx   = choice.get("sub",   source.get("DefaultSubtitleStreamIndex"))

        audio_tracks, sub_tracks = jellyfin.track_lists(source, audio_idx, sub_idx)
        # track_lists may veto an image-based default subtitle; respect that
        # when deciding whether the transcode tier should burn subs in.
        selected_sub = next((t for t in sub_tracks if t["selected"] and t["id"]), None)
        burn_sub_idx = jellyfin.stream_index(selected_sub["id"]) if selected_sub else None

        common = {
            "mediaSourceId": msid,
            "api_key":       jf.token,
            "deviceId":      DEVICE_ID,
        }
        if session:
            common["playSessionId"] = session

        # ── Three playback URLs for the three-tier decision in the frontend ──
        # Tier 1 — Direct play: the original file, byte for byte, zero server CPU
        direct_url = (f"{JELLYFIN_URL}/Videos/{rating_key}/stream?"
                      + urlencode({**common, "static": "true"}))

        hls_common = {
            **common,
            "audioCodec":                      "aac",
            "maxAudioChannels":                MAX_AUDIO_CHANNELS,
            "allowAudioStreamCopy":            "false",
            # One quality per stream: an ABR ladder would spawn a second
            # transcode the moment hls.js switched rung, and on a LAN there is
            # nothing to adapt to anyway.
            "enableAdaptiveBitrateStreaming":  "false",
            # No server-side subtitles by default.  Text subs are sideloaded
            # and rendered client-side so playback is never interrupted, and
            # letting Jellyfin burn in a file's default subtitle would force a
            # video re-encode behind our back.
            "subtitleStreamIndex":             -1,
        }
        if audio_idx is not None:
            hls_common["audioStreamIndex"] = audio_idx

        # Jellyfin sizes the picture to fit the bitrate it is handed, and its
        # fallback when handed nothing is 256 kbps — which silently turns a 4K
        # source into a 416x177 stream.  Always state a budget.
        src_bitrate = jellyfin.source_bitrate(source)

        # Tier 2 — Direct stream: remux the container and convert only the
        #           audio; the video track is copied untouched.
        #
        #           fMP4 segments, not MPEG-TS: hls.js can only demux H.264 in
        #           TS, so copying an HEVC/AV1/VP9 track into TS segments gives
        #           a stream the player silently fails on.  fMP4 carries all of
        #           them, which matters for any x265 library.
        direct_stream_url = (f"{JELLYFIN_URL}/Videos/{rating_key}/master.m3u8?"
                             + urlencode({
                                 **hls_common,
                                 "segmentContainer":     "mp4",
                                 "videoCodec":           "h264,hevc,vp9,av1",
                                 "allowVideoStreamCopy": "true",
                                 "enableAutoStreamCopy": "true",
                                 # A copy can't exceed the source, so the
                                 # ceiling just has to be out of the way — it
                                 # is what makes Jellyfin advertise the real
                                 # bitrate instead of its 256 kbps default.
                                 "videoBitRate":         MAX_STREAMING_BITRATE,
                             }))

        # Tier 3 — Full transcode: re-encode the video, and burn in the
        #           selected subtitle (the only way to show image-based subs).
        #           Always H.264 here, so plain MPEG-TS is the safest carrier.
        tc = {
            **hls_common,
            "segmentContainer":     "ts",
            "videoCodec":           "h264",
            "allowVideoStreamCopy": "false",
            "enableAutoStreamCopy": "false",
        }
        max_w, max_h = _parse_resolution(target_resolution)
        if max_w and max_h:
            tc["maxWidth"], tc["maxHeight"] = max_w, max_h
        # Budget for the re-encode: what the caller asked for, else match the
        # source so "original" really means original.
        budget = None
        if max_bitrate:
            try:
                budget = int(max_bitrate) * 1000
            except ValueError:
                budget = None
        if budget is None:
            budget = src_bitrate or MAX_STREAMING_BITRATE
        tc["videoBitRate"] = min(budget, MAX_STREAMING_BITRATE)
        if burn_sub_idx is not None:
            tc["subtitleStreamIndex"] = burn_sub_idx
            tc["subtitleMethod"]      = "Encode"
        transcode_url = (f"{JELLYFIN_URL}/Videos/{rating_key}/master.m3u8?"
                         + urlencode(tc))

        # ── Intro / credits markers (for Skip Intro / Skip Credits) ──
        runtime_ms = jellyfin.ticks_to_ms(source.get("RunTimeTicks"))
        markers = []
        try:
            seg = jf.get_json(f"/MediaSegments/{rating_key}") or {}
            markers = jellyfin.to_markers(seg.get("Items"), runtime_ms)
        except Exception:
            pass   # no segments for this item — Skip buttons simply don't appear

        # ── Alternate versions, for the quality menu ─────────────────────────
        versions = [{
            "id":       s.get("Id"),
            "label":    jellyfin.source_label(s),
            "selected": s.get("Id") == msid,
        } for s in sorted(sources, key=jellyfin.source_rank, reverse=True)]

        return jsonify({
            "direct":         direct_url,         # Tier 1: raw file
            "directStream":   direct_stream_url,  # Tier 2: remux audio only
            "transcode":      transcode_url,      # Tier 3: full re-encode
            "duration":       runtime_ms,
            "quality":        jellyfin.source_quality(source),
            "audioTracks":    audio_tracks,
            "subTracks":      sub_tracks,
            "partId":         msid,
            "mediaSourceId":  msid,
            "versions":       versions if len(versions) > 1 else [],
            "markers":        markers,
        })
    except Exception as e:
        log.warning("stream_url %s failed: %s", rating_key, e)
        return jsonify({"error": safe_error(e)}), 500


# ── Pick active audio / subtitle stream ────────────────────────────────────
@app.route("/api/select_stream", methods=["POST"])
def api_select_stream():
    """
    Remember which audio / subtitle track to use for a file.

    Jellyfin takes the track as a stream-URL parameter rather than through a
    "set the active stream" call, so the choice is stored server-side and
    applied every time a stream URL is built for that media source.
    """
    body    = request.json or {}
    part_id = body.get("part_id")
    audio   = body.get("audio_id")
    sub     = body.get("sub_id")

    if not part_id:
        return jsonify({"error": "part_id required"}), 400
    if audio is None and sub is None:
        return jsonify({"error": "audio_id or sub_id required"}), 400

    choice = dict(_stream_choices.get(part_id, {}))
    if audio is not None:
        idx = jellyfin.stream_index(audio)
        if idx is not None:
            choice["audio"] = idx
    if sub is not None:
        idx = jellyfin.stream_index(sub)
        # -1 records an explicit "Off", which must survive a reload rather
        # than falling back to the file's default subtitle.
        choice["sub"] = idx if idx is not None else -1

    _remember(_stream_choices, part_id, choice)
    return jsonify({"ok": True})


# ── Subtitles ──────────────────────────────────────────────────────────────
# Jellyfin converts text subtitles (SRT/ASS/embedded) to WebVTT itself, so the
# TV page can render them over Direct Play without interrupting the video.
# Image-based formats (PGS/VobSub…) can only be burned in by the transcoder.
@app.route("/api/subtitle_vtt/<rating_key>/<int:stream_id>")
def api_subtitle_vtt(rating_key, stream_id):
    """Return the given subtitle track as WebVTT, or 409 if it can only be
    burned in."""
    index = jellyfin.stream_index(stream_id)
    if index is None:
        return jsonify({"error": "invalid subtitle stream"}), 400

    try:
        # Resolve against the exact file the player is on — a title with
        # several versions has a different stream layout in each.
        ctx = _active_source.get(rating_key) or {}
        pbi = jf.get_json(f"/Items/{rating_key}/PlaybackInfo",
                          jf.user_params()) or {}
        source = jellyfin.pick_source(pbi.get("MediaSources"), ctx.get("msid"))
        if not source:
            return jsonify({"error": "no playable media source"}), 404
        msid = source.get("Id") or rating_key

        stream = jellyfin.find_stream(source, index)
        if not stream:
            return jsonify({"error": "subtitle stream not found"}), 404

        codec = jellyfin.normalize_sub_codec(stream.get("Codec"))
        if codec in jellyfin.IMAGE_SUB_CODECS or not stream.get("IsTextSubtitleStream"):
            return jsonify({"error": "image-based subtitles need burn-in",
                            "imageBased": True}), 409

        r = jf.request("GET",
                       f"/Videos/{rating_key}/{msid}/Subtitles/{index}/Stream.vtt",
                       timeout=30)
        # Jellyfin prefixes the VTT with a UTF-8 BOM, which makes the browser's
        # parser reject the WEBVTT signature line.
        vtt = r.content.decode("utf-8-sig", errors="replace")
        return Response(vtt, content_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        log.warning("subtitle %s/%s failed: %s", rating_key, stream_id, e)
        return jsonify({"error": safe_error(e)}), 500


# ── Mark watched / unwatched ───────────────────────────────────────────────
@app.route("/api/mark_watched", methods=["POST"])
def api_mark_watched():
    body = request.json or {}
    rating_key = body.get("rating_key")
    watched    = bool(body.get("watched", True))
    if not rating_key:
        return jsonify({"error": "rating_key required"}), 400
    try:
        jf.request("POST" if watched else "DELETE",
                   f"/UserPlayedItems/{rating_key}", params=jf.user_params())
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": safe_error(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  YouTube app — ad-free playback via yt-dlp + ffmpeg
#  Videos are resolved to direct media streams (no YouTube page, no ads) and
#  remuxed on the fly into fragmented MP4 that the browser <video> can play.
# ═══════════════════════════════════════════════════════════════════════════

YT_DLP  = shutil.which("yt-dlp")
_FFMPEG = shutil.which("ffmpeg")

# Resolved stream URLs are signed and expire after a few hours — cache briefly
_yt_cache = {}
_YT_CACHE_TTL = 30 * 60   # seconds

# Ceiling on YouTube resolution (2160 = 4K). Override in config.py if your PC
# can't comfortably decode 4K — 1440 or 1080 are lighter.
try:
    from config import YT_MAX_HEIGHT
except ImportError:
    YT_MAX_HEIGHT = 2160

# Format ladder, best-first. YouTube only offers >1080p in VP9/AV1 (never
# H.264), so to reach 1440p/4K we must accept those. VP9 is preferred — it has
# the widest hardware-decode support in Chrome — then AV1, then H.264. Audio is
# muxed as AAC (m4a) so the result is a clean MP4 the <video> tag plays.
_YT_FORMAT = (
    f"bv*[height<={YT_MAX_HEIGHT}][vcodec^=vp9]+ba[ext=m4a]/"
    f"bv*[height<={YT_MAX_HEIGHT}][vcodec^=av01]+ba[ext=m4a]/"
    f"bv*[height<={YT_MAX_HEIGHT}]+ba/"
    f"b[ext=mp4]/b"
)


def _build_ca_bundle():
    """Export the Windows certificate store to a PEM bundle.

    AV suites that inspect HTTPS (AVG/Avast/etc.) re-sign every TLS
    connection with their own root CA. That CA lives in the *Windows* trust
    store, but yt-dlp ships its own CA list — so on such machines every
    yt-dlp request dies with CERTIFICATE_VERIFY_FAILED. Exporting the
    Windows store (public CAs + the AV root) and pointing the usual env vars
    at it makes children trust exactly what Windows trusts.
    """
    if sys.platform != "win32":
        return None
    path = os.path.join(tempfile.gettempdir(), "jht_windows_ca.pem")
    try:
        import ssl as _ssl
        pems = []
        for store in ("ROOT", "CA"):
            try:
                for der, enc, _trust in _ssl.enum_certificates(store):
                    if enc == "x509_asn":
                        pems.append(_ssl.DER_cert_to_PEM_cert(der))
            except Exception:
                continue
        if not pems:
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(pems))
        return path
    except Exception:
        return None


_CA_BUNDLE = _build_ca_bundle()

# Our own outbound HTTPS (e.g. SponsorBlock) hits the same AV interception —
# let requests use the Windows-store bundle too unless the user set their own.
if _CA_BUNDLE:
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA_BUNDLE)


def _net_env():
    """Environment for child processes that talk to the internet
    (yt-dlp / ffmpeg): point every common CA variable at the Windows-store
    bundle so AV HTTPS-scanning proxies don't break TLS verification."""
    env = dict(os.environ)
    if _CA_BUNDLE:
        env["SSL_CERT_FILE"]      = _CA_BUNDLE
        env["REQUESTS_CA_BUNDLE"] = _CA_BUNDLE
        env["CURL_CA_BUNDLE"]     = _CA_BUNDLE
    return env


def _ytdlp_json(args, timeout=60):
    # --compat-options no-certifi: trust the SYSTEM certificate store.
    # AV suites (AVG/Avast…) that scan HTTPS re-sign traffic with a root CA
    # that only exists in the Windows store — yt-dlp's bundled certifi list
    # rejects it and every request fails CERTIFICATE_VERIFY_FAILED.
    proc = subprocess.run(
        [YT_DLP, "--no-warnings", "--compat-options", "no-certifi", "-J"] + args,
        capture_output=True, timeout=timeout, env=_net_env(),
    )
    # yt-dlp can exit non-zero when single entries fail while still printing
    # a valid JSON document — use the output if there is one.
    if proc.stdout.strip():
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
    raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[:300])


def _yt_resolve(video_id):
    """Resolve a video to direct stream URLs (cached)."""
    now = time.time()
    cached = _yt_cache.get(video_id)
    if cached and now - cached["ts"] < _YT_CACHE_TTL:
        return cached["info"]

    # No forced player_client. This used to pin `android_vr`, on the grounds
    # that its URLs were the ones an external fetcher like ffmpeg could
    # download. That distinction is gone — googlevideo now refuses the
    # open-ended range ffmpeg asks for whatever client resolved the URL, which
    # is what `_YtSourceHandler` exists to work around — so the best choice is
    # whatever client set yt-dlp currently maintains as its default.
    data = _ytdlp_json(
        ["-f", _YT_FORMAT, f"https://www.youtube.com/watch?v={video_id}"],
        timeout=90,
    )
    fmts = data.get("requested_formats")
    if fmts:                                   # separate video + audio
        video_url = fmts[0]["url"]
        audio_url = fmts[1]["url"] if len(fmts) > 1 else None
        headers   = fmts[0].get("http_headers", {})
        height    = fmts[0].get("height")
        width     = fmts[0].get("width")
    else:                                      # single progressive file
        video_url = data.get("url")
        audio_url = None
        headers   = data.get("http_headers", {})
        height    = data.get("height")
        width     = data.get("width")

    info = {
        "id":         video_id,
        "title":      data.get("title", ""),
        "channel":    data.get("channel") or data.get("uploader", ""),
        "duration":   int((data.get("duration") or 0) * 1000),   # ms
        "height":     height,
        "width":      width,
        "video_url":  video_url,
        "audio_url":  audio_url,
        "user_agent": headers.get("User-Agent", ""),
    }
    _yt_cache[video_id] = {"ts": now, "info": info}
    return info


# ── googlevideo fetch proxy ─────────────────────────────────────────────────
# ffmpeg cannot read a googlevideo URL directly, and neither can anything else
# that just opens a URL and reads. googlevideo serves media only through its
# own chunked protocol:
#
#   · the byte range goes in the QUERY STRING as &range=START-END, not in a
#     Range header — a `Range: bytes=0-` header is answered with 403;
#   · each request carries &rn=N, a request counter that starts at 0 and
#     increments, plus &rbuf=0;
#   · a stream must be read FROM THE START, sequentially. A first request at
#     any non-zero offset is refused however rn is set, so byte-range seeking
#     into the middle of a file is simply not available;
#   · one request may cover only a slice of the file (about a tenth).
#
# This handler speaks that protocol upstream and presents an ordinary
# sequential HTTP stream to ffmpeg. It runs on its own loopback server rather
# than as a Flask route, so a long stream can never tie up a worker the page
# itself needs.

_YT_CHUNK_MAX = 4 << 20     # ceiling per upstream request
_YT_CHUNK_MIN = 256 << 10
_YT_CHUNK_DIVISOR = 12      # keeps each request well under the ~10% limit


def _yt_chunk_size(total):
    if not total:
        return _YT_CHUNK_MIN
    return max(_YT_CHUNK_MIN, min(_YT_CHUNK_MAX, total // _YT_CHUNK_DIVISOR))


def _yt_range_url(url, start, end, rn):
    """Add googlevideo's chunk parameters, replacing any already present."""
    base, _, query = url.partition("?")
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
              if k not in ("range", "rn", "rbuf")]
    params += [("range", f"{start}-{end}"), ("rn", str(rn)), ("rbuf", "0")]
    return f"{base}?{urlencode(params)}"


class _YtSourceHandler(BaseHTTPRequestHandler):
    """Serves /<video_id>/<v|a> by relaying one googlevideo stream."""

    protocol_version = "HTTP/1.1"
    server_version = "JellyfinHomeTheaterYT/1.0"

    def log_message(self, *args):
        pass        # the app's own logger reports what matters

    def _target(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or parts[1] not in ("v", "a"):
            return None, None
        try:
            info = _yt_resolve(parts[0])
        except Exception as exc:
            logging.getLogger("yt").warning("proxy resolve failed: %s", exc)
            return None, None
        url = info["video_url"] if parts[1] == "v" else info["audio_url"]
        return url, info.get("user_agent") or ""

    def _fetch(self, session, url, headers, start, end, rn, attempts=3):
        """One upstream chunk, retried through transient refusals.

        Reading hard for a while earns a spell of 403s on chunks that would
        normally be served, so a refusal is worth waiting out briefly rather
        than treating as the end of the stream.
        """
        delay = 0.6
        for attempt in range(attempts):
            try:
                r = session.get(_yt_range_url(url, start, end, rn),
                                headers=headers, stream=True, timeout=30)
            except requests.RequestException as exc:
                if attempt == attempts - 1:
                    logging.getLogger("yt").warning("proxy give up at %s: %s",
                                                    start, exc)
                    return None
            else:
                if r.status_code in (200, 206):
                    return r
                r.close()
                if r.status_code not in (403, 429, 500, 502, 503):
                    logging.getLogger("yt").warning(
                        "proxy upstream %s at byte %s", r.status_code, start)
                    return None
                if attempt == attempts - 1:
                    logging.getLogger("yt").warning(
                        "proxy upstream %s at byte %s (gave up)",
                        r.status_code, start)
                    return None
            time.sleep(delay)
            delay *= 2
        return None

    def do_HEAD(self):
        self._serve(body=False)

    def do_GET(self):
        self._serve(body=True)

    def _serve(self, body=True):
        url, user_agent = self._target()
        if not url:
            self.send_error(404)
            return

        session = requests.Session()
        headers = {"User-Agent": user_agent} if user_agent else {}

        # The first chunk doubles as the size probe — Content-Range carries the
        # total. Asking for a single byte first would waste a request, and
        # requests are exactly what googlevideo rations.
        first = self._fetch(session, url, headers, 0, _YT_CHUNK_MIN - 1, 0)
        if first is None:
            session.close()
            self.send_error(502)
            return
        content_range = first.headers.get("Content-Range", "")
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                total = 0

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        # Deliberately not seekable: upstream only serves a stream read from
        # the start, so inviting ffmpeg to request a byte offset would just
        # earn a 403 storm. It skips by demuxing forward instead.
        self.send_header("Accept-Ranges", "none")
        if total:
            self.send_header("Content-Length", str(total))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        if not body:
            first.close()
            session.close()
            return

        chunk = _yt_chunk_size(total)
        pos, rn = 0, 1
        try:
            r = first
            while True:
                served = 0
                for block in r.iter_content(64 * 1024):
                    if not block:
                        break
                    self.wfile.write(block)
                    served += len(block)
                r.close()
                if not served:
                    break
                pos += served
                if total and pos >= total:
                    break
                end = pos + chunk - 1
                if total:
                    end = min(end, total - 1)
                r = self._fetch(session, url, headers, pos, end, rn)
                if r is None:
                    break
                rn += 1
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                OSError):
            pass        # ffmpeg stopped reading (seek, or playback ended)
        finally:
            session.close()


_yt_proxy = {"port": None}
_yt_proxy_lock = threading.Lock()


def _yt_proxy_port():
    """Start the loopback fetch proxy on first use and return its port."""
    with _yt_proxy_lock:
        if _yt_proxy["port"] is None:
            server = ThreadingHTTPServer(("127.0.0.1", 0), _YtSourceHandler)
            server.daemon_threads = True
            _yt_proxy["port"] = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True,
                             name="ytSourceProxy").start()
            logging.getLogger("yt").info(
                "googlevideo fetch proxy listening on 127.0.0.1:%s",
                _yt_proxy["port"])
        return _yt_proxy["port"]


@app.route("/api/yt/search")
def yt_search():
    if not YT_DLP:
        return jsonify({"error": "yt-dlp is not installed"}), 501
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    n = min(int(request.args.get("n", 24)), 40)
    try:
        data = _ytdlp_json(["--flat-playlist", f"ytsearch{n}:{q}"], timeout=60)
        out = []
        for e in data.get("entries", []) or []:
            # keep plain videos only — searches also surface channels/playlists
            if not e or not e.get("id") or e.get("ie_key") != "Youtube":
                continue
            out.append({
                "id":       e["id"],
                "title":    e.get("title", ""),
                "channel":  e.get("channel") or e.get("uploader", ""),
                "duration": int((e.get("duration") or 0) * 1000),  # ms
                "views":    e.get("view_count"),
                "thumb":    f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg",
            })
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": safe_error(e)}), 500


@app.route("/api/yt/resolve/<video_id>")
def yt_resolve(video_id):
    if not YT_DLP:
        return jsonify({"error": "yt-dlp is not installed"}), 501
    try:
        info = _yt_resolve(video_id)
    except Exception as e:
        return jsonify({"error": safe_error(e)}), 500

    # SponsorBlock segments → same shape as Jellyfin media segments, so the
    # TV's existing Skip button works on them unchanged.
    markers = []
    try:
        sb = requests.get(
            "https://sponsor.ajay.app/api/skipSegments",
            params={"videoID": video_id,
                    "categories": json.dumps(
                        ["sponsor", "selfpromo", "intro", "outro"])},
            timeout=5,
        )
        if sb.status_code == 200:
            for seg in sb.json():
                markers.append({
                    "type":  seg.get("category", "sponsor"),
                    "start": int(seg["segment"][0] * 1000),
                    "end":   int(seg["segment"][1] * 1000),
                    "final": False,
                })
    except Exception:
        pass   # SponsorBlock down / no segments — not fatal

    return jsonify({
        "id":       info["id"],
        "title":    info["title"],
        "channel":  info["channel"],
        "duration": info["duration"],
        "height":   info["height"],
        "width":    info.get("width"),
        "remux":    bool(info["audio_url"]),
        "markers":  markers,
    })


@app.route("/api/yt/stream/<video_id>")
def yt_stream(video_id):
    """Remux the resolved video+audio streams into fragmented MP4 on the fly.
    Supports ?t=<seconds> for seeking (ffmpeg starts both inputs there)."""
    if not YT_DLP or not _FFMPEG:
        return jsonify({"error": "yt-dlp and ffmpeg are required"}), 501
    try:
        t = max(0.0, float(request.args.get("t", 0) or 0))
        info = _yt_resolve(video_id)
    except Exception as e:
        return jsonify({"error": safe_error(e)}), 500

    # ffmpeg reads through the loopback fetch proxy, never googlevideo
    # directly — see _YtSourceHandler for why that indirection is required.
    port = _yt_proxy_port()
    src = lambda kind: f"http://127.0.0.1:{port}/{video_id}/{kind}"

    # Per-input options. The reconnect flags are essential: Chrome buffers far
    # ahead then stops reading, so ffmpeg's output pipe blocks and it stops
    # pulling from the source — which then drops the idle connection. Without
    # reconnect, ffmpeg hits EOF and exits, permanently ending the stream
    # (playback freezes mid-video and Play can't revive it). These flags make
    # ffmpeg transparently reopen the HTTP source (with a byte range) and
    # continue where it left off.
    def _in_opts():
        opts = ["-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_on_network_error", "1",
                "-reconnect_on_http_error", "4xx,5xx",
                "-reconnect_delay_max", "5",
                # The proxy can't serve a byte offset (see _YtSourceHandler),
                # so tell ffmpeg up front rather than letting it try.
                "-seekable", "0"]
        if t:
            # With a non-seekable input this skips forward by demuxing, which
            # means waiting for the stream up to that point — YouTube gives no
            # way to start reading mid-file.
            opts += ["-ss", f"{t:.3f}"]
        return opts

    cmd = [_FFMPEG, "-v", "error", "-nostdin"]
    cmd += _in_opts() + ["-i", src("v")]
    if info["audio_url"]:
        cmd += _in_opts() + ["-i", src("a"), "-map", "0:v:0", "-map", "1:a:0"]
    cmd += [
        "-c", "copy",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_net_env())

    # Drain stderr in the background so the pipe never fills (which would stall
    # ffmpeg) and so real failures are visible in the log instead of silent.
    def _drain_stderr():
        try:
            for line in iter(proc.stderr.readline, b""):
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    logging.getLogger("yt").warning("ffmpeg[%s]: %s", video_id, msg)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True, name="ytFfmpegErr").start()

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.kill()

    return Response(generate(), content_type="video/mp4",
                    headers={"Cache-Control": "no-store"})


# ── WLED ambient lighting ───────────────────────────────────────────────────
# SignalRGB drives the strip by streaming to it over UDP, which puts WLED in
# "realtime" mode: the LEDs follow the stream and WLED ignores its own on/off
# and brightness. That is what the WLED app is reporting when it says another
# device is controlling the lights — it is working as designed, not a fault.
#
# WLED exposes a switch for precisely this situation, `lor` (live override):
#
#   lor 0  realtime data wins          → SignalRGB's ambilight
#   lor 1  ignore it until the next preset change
#   lor 2  ignore it until reboot      → local control wins again
#
# So the three modes below cover what you actually want from a light switch,
# without having to quit SignalRGB or open its window.
try:
    from config import WLED_IP
except ImportError:
    WLED_IP = None
try:
    from config import WLED_USE_LIVE_OVERRIDE
except ImportError:
    WLED_USE_LIVE_OVERRIDE = True

WLED_MODES = {
    # mode        → state to send
    "ambilight": {"lor": 0, "on": True},
    "manual":    {"lor": 2, "on": True},
    "off":       {"lor": 2, "on": False},
}


def wled_hosts():
    """WLED_IP accepts a single address or a list of them."""
    if not WLED_IP:
        return []
    if isinstance(WLED_IP, str):
        return [WLED_IP.strip()] if WLED_IP.strip() else []
    return [str(h).strip() for h in WLED_IP if str(h).strip()]


def _wled_fanout(fn):
    """Run fn(host) against every controller at once.

    With more than one strip, doing these in sequence means the lights visibly
    change one after another, and one unplugged controller would hold up the
    rest until it times out.
    """
    hosts = wled_hosts()
    if not hosts:
        return []
    with ThreadPoolExecutor(max_workers=max(2, len(hosts))) as pool:
        return list(pool.map(fn, hosts))


def _wled_read(host):
    try:
        r = requests.get(f"http://{host}/json", timeout=2.5)
        d = r.json()
        st, info = d.get("state") or {}, d.get("info") or {}
        override = st.get("lor", 0)
        return {
            "host":     host,
            "name":     info.get("name") or host,
            "on":       bool(st.get("on")),
            "bri":      st.get("bri"),
            "preset":   st.get("ps"),
            # `live` means a realtime source is streaming right now; `lm`/`lip`
            # say which protocol and from where (SignalRGB shows as UDP from
            # this PC). `override` is whether we are ignoring it.
            "live":     bool(info.get("live")),
            "liveMode": info.get("lm") or "",
            "liveFrom": info.get("lip") or "",
            "override": override,
            "mode":     _wled_mode_of(st.get("on"), override),
            "leds":     (info.get("leds") or {}).get("count"),
            "version":  info.get("ver"),
        }
    except Exception as e:
        return {"host": host, "name": host, "error": str(e)}


def _wled_mode_of(on, override):
    if not on:
        return "off"
    return "ambilight" if not override else "manual"


def _wled_write(host, payload):
    try:
        r = requests.post(f"http://{host}/json/state", json=payload, timeout=3)
        return {"host": host, "ok": r.status_code == 200}
    except Exception as e:
        return {"host": host, "ok": False, "error": str(e)}


def _ha_read_all():
    """Ceiling lights, shaped like the WLED entries so the UI needs no
    special case. `host` is the entity id, which /api/wled/set routes on."""
    out = []
    for entity in ha_lights():
        try:
            st = hass.light_state(entity)
            out.append({"host": entity, "kind": "ceiling",
                        "name": st["name"], "on": st["on"],
                        "bri": st["brightness"], "live": False,
                        "liveMode": "", "liveFrom": "", "override": 0,
                        "mode": None, "leds": None, "version": None})
        except Exception as e:
            out.append({"host": entity, "kind": "ceiling", "name": entity,
                        "error": safe_error(e)})
    return out


@app.route("/api/wled/state")
def wled_state():
    hosts = wled_hosts()
    ceiling = _ha_read_all()
    if not hosts:
        if not ceiling:
            return jsonify({"configured": False, "devices": []})
        # Ceiling lights but no strips: still a usable Lights tab.
        return jsonify({"configured": True, "devices": ceiling, "mode": None,
                        "anyOn": any(d.get("on") for d in ceiling),
                        "anyLive": False, "bri": None,
                        "overrideAvailable": False,
                        "autoDim": _lights_auto["enabled"],
                        "autoScene": _lights_auto["scene"]})
    devices = _wled_fanout(_wled_read)
    for d in devices:
        d["kind"] = "wled"
    reachable = [d for d in devices if "error" not in d]
    modes = {d["mode"] for d in reachable}
    return jsonify({
        "configured":   True,
        "devices":      devices + ceiling,
        # A single mode only when every reachable strip agrees on one.
        "mode":         modes.pop() if len(modes) == 1 else None,
        "anyOn":        any(d["on"] for d in reachable),
        "anyLive":      any(d["live"] for d in reachable),
        "bri":          max((d["bri"] or 0) for d in reachable) if reachable else None,
        "overrideAvailable": WLED_USE_LIVE_OVERRIDE,
        "autoDim":      _lights_auto["enabled"],
        "autoScene":    _lights_auto["scene"],
    })


@app.route("/api/wled/set", methods=["POST"])
def wled_set():
    """Forward a WLED JSON-state payload: {on, bri, ps, lor} — all optional.

    `host` restricts it to one controller; by default every one is set.
    """
    if not wled_hosts() and not ha_lights():
        return jsonify({"error": "no lights configured in config.py"}), 501
    body = request.get_json(silent=True) or {}
    payload = {k: body[k] for k in ("on", "bri", "ps", "lor") if k in body}
    if not payload:
        return jsonify({"error": "nothing to set"}), 400

    one = body.get("host")
    if one and one in ha_lights():
        # A ceiling light, not a strip.
        try:
            bri = payload.get("bri")
            hass.set_light(one, bool(payload.get("on", True)),
                           bri if bri is not None else None)
            return jsonify({"ok": True, "results": [{"host": one, "ok": True}]})
        except Exception as e:
            return jsonify({"ok": False, "error": safe_error(e)}), 502
    if one:
        if one not in wled_hosts():
            return jsonify({"error": "unknown host"}), 400
        results = [_wled_write(one, payload)]
    else:
        results = _wled_fanout(lambda h: _wled_write(h, payload))
    return jsonify({"ok": any(r.get("ok") for r in results), "results": results})


# ── Dim the lights when something plays ─────────────────────────────────────
# The TV reports its state over the socket on every play, pause and screen
# change (for library content and the YouTube app alike), which is all this
# needs: screen + playing collapse into one of three scenes, and a change of
# scene moves the lights.
#
# Deliberately only acts on a *change*. Grab the remote mid-film and set
# whatever you like — nothing here will argue with you until the next time you
# pause or stop.
try:
    from config import DIM_ON_PLAYBACK
except ImportError:
    DIM_ON_PLAYBACK = False
try:
    from config import LIGHTS_PLAYING_BRI
except ImportError:
    LIGHTS_PLAYING_BRI = 40
try:
    from config import LIGHTS_PAUSED_BRI
except ImportError:
    LIGHTS_PAUSED_BRI = 120
try:
    from config import LIGHTS_IDLE_BRI
except ImportError:
    LIGHTS_IDLE_BRI = None
try:
    from config import LIGHTS_AMBILIGHT_WHILE_PLAYING
except ImportError:
    LIGHTS_AMBILIGHT_WHILE_PLAYING = True

# ── Ceiling lights, through Home Assistant ──────────────────────────────────
# A second set of lights the same scenes drive. The ceiling and the ambilight
# want different levels — the ambilight stays visible while the ceiling drops
# right down — so they get their own brightness settings.
try:
    from config import HA_URL, HA_TOKEN
except ImportError:
    HA_URL, HA_TOKEN = "", ""
try:
    from config import HA_LIGHTS
except ImportError:
    HA_LIGHTS = []
try:
    from config import HA_PLAYING_PCT
except ImportError:
    HA_PLAYING_PCT = 15
try:
    from config import HA_PAUSED_PCT
except ImportError:
    HA_PAUSED_PCT = 50
try:
    from config import HA_IDLE_PCT
except ImportError:
    HA_IDLE_PCT = None

hass = HomeAssistantClient(HA_URL, HA_TOKEN)


def ha_lights():
    """The ceiling lights to drive, if Home Assistant is set up at all."""
    if not hass.configured or not HA_LIGHTS:
        return []
    if isinstance(HA_LIGHTS, str):
        return [HA_LIGHTS] if HA_LIGHTS.strip() else []
    return [e for e in HA_LIGHTS if e]

# How long the TV can go quiet mid-playback before the lights come back up.
# Without this, closing the TV page while a film is on would leave the room
# dark with nothing left to restore it.
_LIGHTS_TV_TIMEOUT = 90

_lights_auto = {
    "enabled":   bool(DIM_ON_PLAYBACK),
    "scene":     None,     # last scene applied: playing | paused | idle
    "restore":   None,     # {bri, lor} captured before the first dim
    "last_seen": 0.0,      # when the TV last reported a non-idle state
}
_lights_lock = threading.Lock()


def playback_scene(snapshot):
    """Reduce the TV's reported state to a lighting scene."""
    if snapshot.get("screen") != "player":
        return "idle"
    return "playing" if snapshot.get("playing") else "paused"


def _wled_scene_payload(scene, restore):
    """The WLED state for one scene, or None to leave the strips alone."""
    if scene == "idle":
        payload = {"on": True}
        if LIGHTS_IDLE_BRI is not None:
            payload["bri"] = LIGHTS_IDLE_BRI
        elif restore:
            payload.update(restore)
        else:
            return None
        return payload

    bri = LIGHTS_PLAYING_BRI if scene == "playing" else LIGHTS_PAUSED_BRI
    payload = {"on": False} if bri <= 0 else               {"on": True, "bri": max(1, min(255, int(bri)))}
    # While playing, leaving the override off keeps SignalRGB driving the
    # strips — WLED scales the incoming stream by the master brightness, so
    # they still follow the screen, just dimmer.
    if WLED_USE_LIVE_OVERRIDE:
        payload["lor"] = 0 if (scene == "playing" and
                               LIGHTS_AMBILIGHT_WHILE_PLAYING) else 2
    return payload


def _apply_ha_scene(scene, restore):
    """Move the ceiling lights for one scene.

    The ceiling gets its own levels: it drops much further than the ambilight,
    which is the whole point of having both.
    """
    entities = ha_lights()
    if not entities:
        return

    for entity in entities:
        try:
            if scene == "idle":
                previous = (restore or {}).get(entity)
                if HA_IDLE_PCT is not None:
                    hass.set_light(entity, True, pct_to_255(HA_IDLE_PCT))
                elif previous:
                    hass.set_light(entity, previous["on"], previous["brightness"])
                continue
            pct = HA_PLAYING_PCT if scene == "playing" else HA_PAUSED_PCT
            if pct <= 0:
                hass.set_light(entity, False)
            else:
                hass.set_light(entity, True, pct_to_255(pct))
        except Exception as exc:
            log.warning("ceiling light %s failed: %s", entity, safe_error(exc))


def _lights_apply_scene(scene, restore):
    """Send one scene to every light we drive. Runs off the socket thread.

    A backend with no entry in the snapshot was off when playback began, so it
    is skipped for the dimming scenes — the automation never switches on a
    light you had deliberately turned off. The two are judged separately, so
    ambilight-off with the ceiling on (or the reverse) does the right thing
    for each.
    """
    restore = restore or {}
    wled_before = restore.get("wled")
    ha_before = restore.get("ha")

    if wled_hosts() and (scene == "idle" or wled_before is not None):
        payload = _wled_scene_payload(scene, wled_before)
        if payload:
            log.info("lights → %s  strips %s", scene, payload)
            _wled_fanout(lambda h: _wled_write(h, payload))

    if ha_lights() and (scene == "idle" or ha_before is not None):
        log.info("lights → %s  ceiling", scene)
        _apply_ha_scene(scene, ha_before)


def _capture_lights():
    """Where every light stands right now, to be put back afterwards.

    A light that is already off is recorded as `None` rather than snapshotted,
    which tells the scene applier to leave it alone: if you deliberately
    switched something off, starting a film is no reason to switch it on at a
    dim setting. The two backends are judged separately, so ambilight-off with
    the ceiling on (or the reverse) does the sensible thing for each.
    """
    snapshot = {"wled": None, "ha": None}

    if wled_hosts():
        current = next((d for d in _wled_fanout(_wled_read)
                        if "error" not in d), None)
        if current and current["on"]:
            snapshot["wled"] = {"bri": current["bri"], "lor": current["override"]}

    entities = ha_lights()
    if entities:
        states = {}
        for entity in entities:
            try:
                st = hass.light_state(entity)
                if st["on"]:
                    states[entity] = {"on": True, "brightness": st["brightness"]}
            except Exception as exc:
                log.warning("could not read %s: %s", entity, safe_error(exc))
        if states:
            snapshot["ha"] = states

    return snapshot


def note_playback_state(snapshot):
    """Move the lights if the playback scene changed. Cheap and non-blocking."""
    if not _lights_auto["enabled"]:
        return
    if not wled_hosts() and not ha_lights():
        return
    scene = playback_scene(snapshot)

    with _lights_lock:
        if scene != "idle":
            _lights_auto["last_seen"] = time.time()
        if scene == _lights_auto["scene"]:
            return
        previous = _lights_auto["scene"]
        _lights_auto["scene"] = scene
        restore = _lights_auto["restore"]

        if scene == "idle":
            _lights_auto["restore"] = None
        elif previous in (None, "idle") and restore is None:
            # First dip into playback: note where everything was.
            restore = _capture_lights()
            _lights_auto["restore"] = restore
            if not any(restore.values()):
                log.info("lights → standing by (nothing was on)")
                return

    threading.Thread(target=_lights_apply_scene, args=(scene, restore),
                     daemon=True, name="lightsScene").start()


def _lights_watchdog():
    while True:
        time.sleep(15)
        try:
            with _lights_lock:
                scene = _lights_auto["scene"]
                stale = time.time() - _lights_auto["last_seen"]
                if scene in (None, "idle") or stale < _LIGHTS_TV_TIMEOUT:
                    continue
                restore = _lights_auto["restore"]
                _lights_auto["scene"] = "idle"
                _lights_auto["restore"] = None
            log.info("lights → idle (TV silent for %ds)", int(stale))
            _lights_apply_scene("idle", restore)
        except Exception:
            pass


if DIM_ON_PLAYBACK:
    threading.Thread(target=_lights_watchdog, daemon=True,
                     name="lightsWatchdog").start()


@app.route("/api/wled/auto", methods=["POST"])
def wled_auto():
    """Turn the dim-on-playback behaviour on or off for this session.

    Switching it off restores the lights if they are currently dimmed, so it
    never leaves the room dark.
    """
    body = request.get_json(silent=True) or {}
    if "enabled" not in body:
        return jsonify({"error": "enabled required"}), 400
    enabled = bool(body["enabled"])

    with _lights_lock:
        _lights_auto["enabled"] = enabled
        restore = _lights_auto["restore"] if not enabled else None
        if not enabled:
            _lights_auto["scene"] = None
            _lights_auto["restore"] = None
    if not enabled and restore:
        threading.Thread(target=_lights_apply_scene, args=("idle", restore),
                         daemon=True, name="lightsScene").start()
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/wled/mode", methods=["POST"])
def wled_mode():
    """Switch the strips between ambilight, manual and off.

    This is the control that makes the lights behave like a light switch even
    while SignalRGB is streaming — see the note at the top of this section.
    """
    if not wled_hosts() and not ha_lights():
        return jsonify({"error": "no lights configured in config.py"}), 501
    mode = (request.get_json(silent=True) or {}).get("mode")
    if mode not in WLED_MODES:
        return jsonify({"error": f"mode must be one of {sorted(WLED_MODES)}"}), 400

    payload = dict(WLED_MODES[mode])
    if not WLED_USE_LIVE_OVERRIDE:
        # Honour the opt-out: send only on/off and let the stream win.
        payload.pop("lor", None)
    results = _wled_fanout(lambda h: _wled_write(h, payload))

    # The Off button says "Lights out", so it takes the ceiling with it. The
    # other two modes are about SignalRGB's stream and leave the ceiling to
    # its own switch.
    if mode == "off":
        for entity in ha_lights():
            try:
                hass.set_light(entity, False)
            except Exception as e:
                log.warning("ceiling %s: %s", entity, safe_error(e))

    return jsonify({"ok": any(r.get("ok") for r in results) or not wled_hosts(),
                    "mode": mode, "results": results})


# ── Playback reporting — registers an active "Now Playing" session ───────────
# This is what makes the Jellyfin dashboard show the session, drives Continue
# Watching / Next Up, and syncs resume positions with your other clients.
# Called on play start, every ~10 s during playback, on pause, and on stop.
def _stop_transcode(play_session_id):
    """Tell Jellyfin to kill the ffmpeg process behind a finished stream.

    A transcode keeps running for a while after the client walks away, and on
    a box that is also driving the display (and ambient lighting) a stranded
    4K encode is expensive. Harmless when the stream was a Direct Play.
    """
    if not play_session_id:
        return
    try:
        jf.request("DELETE", "/Videos/ActiveEncodings",
                   params={"deviceId": DEVICE_ID,
                           "playSessionId": play_session_id},
                   timeout=5)
    except Exception:
        pass


@app.route("/api/timeline", methods=["POST"])
def playback_progress():
    body       = request.get_json(silent=True) or {}
    rating_key = body.get("rating_key", "")
    play_state = body.get("state", "playing")   # playing | paused | stopped
    mode       = body.get("mode")               # direct | directStream | transcode
    try:
        position = int(body.get("position", 0) or 0)   # ms
    except (TypeError, ValueError):
        position = 0

    if not rating_key:
        return jsonify({"ok": True})

    ctx = _active_source.get(rating_key) or {}
    payload = {
        "ItemId":        rating_key,
        "MediaSourceId": ctx.get("msid") or rating_key,
        "PositionTicks": ms_to_ticks(position),
        "IsPaused":      play_state == "paused",
        "IsMuted":       False,
        "PlayMethod":    PLAY_METHODS.get(mode, "DirectPlay"),
        "CanSeek":       True,
        "RepeatMode":    "RepeatNone",
    }
    if ctx.get("playSessionId"):
        payload["PlaySessionId"] = ctx["playSessionId"]

    try:
        if play_state == "stopped":
            jf.request("POST", "/Sessions/Playing/Stopped", json_body=payload,
                       timeout=5)
            _playing_now.discard(rating_key)
            _stop_transcode(ctx.get("playSessionId"))
        elif rating_key not in _playing_now:
            jf.request("POST", "/Sessions/Playing", json_body=payload, timeout=5)
            _playing_now.add(rating_key)
        else:
            jf.request("POST", "/Sessions/Playing/Progress", json_body=payload,
                       timeout=5)
    except Exception:
        pass   # Non-fatal — don't break playback if Jellyfin blips

    return jsonify({"ok": True})

# ── WebSocket events ──────────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("state", state)

def _apply_command_to_state(data):
    action = data.get("action")
    if action == "play":
        state["playing"] = True
    elif action == "pause":
        state["playing"] = False
    elif action == "stop":
        state["playing"] = False
        state["position"] = 0
    elif action == "volume":
        state["volume"] = data.get("value", state["volume"])


@socketio.on("command")
def on_command(data):
    """Remote sends a command → relay to TV and update state."""
    _apply_command_to_state(data)
    emit("command", data, broadcast=True, include_self=False)
    emit("state", state, broadcast=True)


# ── Commands over HTTP ──────────────────────────────────────────────────────
# Same channel the phone remote uses, reachable without a Socket.IO client.
# This is what lets remote_bridge.py forward a USB remote's keypresses to the
# TV without the kiosk window needing focus — the whole point being that you
# can keep using the PC for something else at the same time.
# Socket ids belonging to TV pages. Identified by the fact that only the TV
# sends state_update — the phone remote never does. Presence is tracked by
# connection rather than by a timestamp because the TV goes quiet whenever
# nothing is changing on screen, which is most of the time while browsing.
_tv_sids = set()
_tv_seen = {"at": 0.0}


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if not action:
        return jsonify({"error": "action required"}), 400
    payload = {"action": action}
    if "value" in body:
        payload["value"] = body["value"]

    _apply_command_to_state(payload)
    socketio.emit("command", payload)
    socketio.emit("state", state)
    return jsonify({"ok": True, "tv": _tv_alive()})


def _tv_alive():
    return bool(_tv_sids)


@app.route("/api/tv_status")
def api_tv_status():
    """Whether a TV page is up, and what it is doing.

    The bridge asks this before swallowing a keypress: with no TV listening
    there is nothing to send the key to, so it should behave like an ordinary
    keyboard instead of disappearing into a void.
    """
    return jsonify({
        "connected":     _tv_alive(),
        "count":         len(_tv_sids),
        "secondsAgo":    round(time.time() - _tv_seen["at"], 1)
                         if _tv_seen["at"] else None,
        "screen":        state.get("screen"),
        "playing":       bool(state.get("playing")),
        "title":         state.get("title") or "",
    })

@socketio.on("state_update")
def on_state_update(data):
    """TV sends its current state → broadcast to all remotes."""
    state.update(data)
    # Only the TV sends this, so it is how a TV page identifies itself. The
    # USB remote bridge asks /api/tv_status whether any are connected before
    # it intercepts anything.
    try:
        _tv_sids.add(request.sid)
    except Exception:
        pass
    _tv_seen["at"] = time.time()
    # Playback started, paused or ended — the lights may want to follow.
    try:
        note_playback_state(state)
    except Exception:
        pass          # never let the lights interfere with playback reporting
    emit("state", state, broadcast=True, include_self=False)

@socketio.on("ping_check")
def on_ping():
    emit("pong_check")


@socketio.on("disconnect")
def on_disconnect(*args):
    """A TV page closing is what makes the remote behave like a keyboard again."""
    try:
        _tv_sids.discard(request.sid)
    except Exception:
        pass

# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Verify the Jellyfin connection on startup
    try:
        info = jf.connect()
        print(f"✅  Connected to Jellyfin: {info.get('ServerName', 'Unknown')} "
              f"(v{info.get('Version', '?')}) as {JELLYFIN_USER}")
    except JellyfinError as e:
        print(f"⚠  {e}")
        print("   The app will still start but your library won't load.")
    except Exception as e:
        print(f"⚠  Could not reach Jellyfin at {JELLYFIN_URL}: {e}")
        print("   The app will still start but your library won't load.")

    # ── Start monitor watcher ────────────────────────────────────────────────
    if MONITOR_NAME:
        from monitor_watcher import start_monitor_watcher
        start_monitor_watcher(
            target_name             = MONITOR_NAME,
            chrome_path             = CHROME_PATH,
            port                    = PORT,
            poll_interval           = MONITOR_POLL_INTERVAL,
            require_active_input    = REQUIRE_ACTIVE_INPUT,
            expected_input_vcp      = EXPECTED_INPUT_VCP,
            fallback_to_power_state = FALLBACK_TO_POWER_STATE,
        )
        print(f"👁  Monitor watcher active — watching for: {MONITOR_NAME}")
        if REQUIRE_ACTIVE_INPUT:
            print(f"   HDMI input check enabled (DDC/CI). "
                  f"Expected VCP value: {EXPECTED_INPUT_VCP if EXPECTED_INPUT_VCP is not None else 'auto-learn'}")
        print(f"   (run  python find_displays.py  to list all connected displays)")
    else:
        print("ℹ  Monitor watcher disabled (MONITOR_NAME is None in config.py)")

    local_ip = "0.0.0.0"
    print(f"\n🎬  Jellyfin Home Theater running!")
    print(f"   TV Display   → http://localhost:{PORT}/tv")
    print(f"   Phone Remote → http://<your-pc-ip>:{PORT}/remote\n")

    socketio.run(app, host=local_ip, port=PORT, debug=False, allow_unsafe_werkzeug=True)
