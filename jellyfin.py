"""
Jellyfin Home Theater — Jellyfin client + data adapter.

Two jobs:

  1. `JellyfinClient` — a small authenticated HTTP client for a Jellyfin
     server.  Logs in with username/password, keeps the access token, and
     silently re-authenticates if the server ever rejects it.

  2. The `to_item` family — translates Jellyfin's item schema into the flat
     dicts the TV frontend consumes (`ratingKey`, `viewOffset`,
     `grandparentTitle`, …).  Keeping the translation here means
     static/js/tv.js needs to know nothing about Jellyfin's schema.

A note on time: Jellyfin measures everything in 100-nanosecond "ticks"
(10 000 per millisecond).  Nothing crosses into the frontend in ticks —
`ticks_to_ms` is applied at the boundary.
"""

import json
import logging
import os
import re
import threading

import requests

log = logging.getLogger("jellyfin")

TICKS_PER_MS = 10_000

# Identifies this app to Jellyfin — shows up in Dashboard → Devices/Sessions.
CLIENT_NAME    = "JellyfinHomeTheater"
CLIENT_VERSION = "1.0"
DEVICE_NAME    = "Home Theater"

_UNSET = object()


def ticks_to_ms(ticks):
    """Jellyfin ticks → milliseconds (0 when absent)."""
    try:
        return int(ticks or 0) // TICKS_PER_MS
    except (TypeError, ValueError):
        return 0


def ms_to_ticks(ms):
    try:
        return int(ms or 0) * TICKS_PER_MS
    except (TypeError, ValueError):
        return 0


class JellyfinError(RuntimeError):
    pass


# ═════════════════════════════════════════════════════════════════════════════
#  HTTP client
# ═════════════════════════════════════════════════════════════════════════════
class JellyfinClient:
    """Authenticated session against one Jellyfin server.

    One pooled `requests.Session` handles all traffic, which matters on the
    home screen: it can ask for a hundred posters at once and a new TCP
    handshake per image would dominate the load time.
    """

    def __init__(self, base_url, username, password, device_id="jellyfin-home-theater",
                 token_cache=None):
        self.base        = (base_url or "").rstrip("/")
        self.username    = username or ""
        self.password    = password or ""
        self.device_id   = device_id
        self.token       = None
        self.user_id     = None
        self.server      = {}          # /System/Info, filled by connect()
        self.session     = requests.Session()
        self.token_cache = token_cache
        self._lock       = threading.Lock()
        self._load_cached_token()

    # ── Token cache ─────────────────────────────────────────────────────────
    # Every AuthenticateByName call opens a *new* Jellyfin session, and an
    # account can carry a "maximum simultaneous sessions" limit (Dashboard →
    # Users).  Logging in on every app restart would eat through that limit
    # until the server starts refusing with "User is at their maximum number
    # of sessions", so the token is reused until the server rejects it — at
    # which point `request()` transparently logs in again.
    def _load_cached_token(self):
        if not self.token_cache or not os.path.isfile(self.token_cache):
            return
        try:
            with open(self.token_cache, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        # Only reuse a token issued for this exact server + account.
        if data.get("url") == self.base and data.get("username") == self.username:
            self.token   = data.get("token")
            self.user_id = data.get("user_id")
            if self.token:
                log.info("Reusing the cached Jellyfin session")

    def _save_cached_token(self):
        if not self.token_cache:
            return
        try:
            with open(self.token_cache, "w", encoding="utf-8") as f:
                json.dump({"url": self.base, "username": self.username,
                           "token": self.token, "user_id": self.user_id}, f)
        except OSError as exc:
            log.debug("Could not write the token cache: %s", exc)

    # ── Auth ────────────────────────────────────────────────────────────────
    def _auth_header(self):
        parts = [f'Client="{CLIENT_NAME}"',
                 f'Device="{DEVICE_NAME}"',
                 f'DeviceId="{self.device_id}"',
                 f'Version="{CLIENT_VERSION}"']
        if self.token:
            parts.append(f'Token="{self.token}"')
        return "MediaBrowser " + ", ".join(parts)

    def login(self, stale_token=_UNSET):
        """Exchange username/password for an access token + user id.

        `stale_token` guards against a thundering herd: when several threads
        get a 401 at once, only the first one actually re-authenticates and
        the rest see that the token already changed.
        """
        with self._lock:
            if stale_token is not _UNSET and self.token and self.token != stale_token:
                return                       # another thread already refreshed
            self.token = None
            try:
                r = self.session.post(
                    f"{self.base}/Users/AuthenticateByName",
                    json={"Username": self.username, "Pw": self.password},
                    headers={"Authorization": self._auth_header(),
                             "Content-Type": "application/json"},
                    timeout=15,
                )
            except requests.RequestException as exc:
                raise JellyfinError(f"Could not reach Jellyfin at {self.base}: {exc}") from exc

            if r.status_code == 401:
                raise JellyfinError(
                    f"Jellyfin rejected the login for user {self.username!r}. "
                    f"Check JELLYFIN_USER / JELLYFIN_PASSWORD in config.py."
                )
            if r.status_code == 403:
                # Almost always the per-user session cap: the password was
                # accepted but the server won't open another session.
                raise JellyfinError(
                    f"Jellyfin refused a new session for {self.username!r} "
                    f"(HTTP 403). This is usually the account's "
                    f"\"Maximum number of simultaneous user sessions\" limit — "
                    f"raise it in Dashboard → Users → {self.username}, or sign "
                    f"out an old device in Dashboard → Devices."
                )
            r.raise_for_status()
            data = r.json()
            self.token   = data.get("AccessToken")
            self.user_id = (data.get("User") or {}).get("Id")
            if not self.token or not self.user_id:
                raise JellyfinError("Jellyfin login succeeded but returned no token/user id.")
            self._save_cached_token()
            log.info("Authenticated with Jellyfin as %s", self.username)

    def connect(self):
        """Make sure we have a working session and fetch server info.

        Returns the /System/Info payload.  A cached token is verified here
        rather than logged in over, so a restart costs no session slot.
        """
        if not self.token:
            self.login()
        self.server = self.get_json("/System/Info") or {}
        return self.server

    # ── Requests ────────────────────────────────────────────────────────────
    def request(self, method, path, params=None, json_body=None,
                stream=False, timeout=20):
        """One authenticated request, retried once through a fresh login if
        the server says the token is no longer valid (server restart, key
        revoked, …)."""
        if not self.token:
            self.login()

        for attempt in (0, 1):
            token_used = self.token
            r = self.session.request(
                method, f"{self.base}{path}",
                params=params, json=json_body, stream=stream, timeout=timeout,
                headers={"Authorization": self._auth_header(),
                         "Accept": "application/json"},
            )
            if r.status_code == 401 and attempt == 0:
                log.info("Jellyfin token rejected — re-authenticating")
                r.close()
                self.login(stale_token=token_used)
                continue
            r.raise_for_status()
            return r
        raise JellyfinError("unreachable")   # pragma: no cover

    def get_json(self, path, params=None, timeout=20):
        r = self.request("GET", path, params=params, timeout=timeout)
        if not r.content:
            return None
        return r.json()

    def post_json(self, path, params=None, json_body=None, timeout=20):
        r = self.request("POST", path, params=params, json_body=json_body, timeout=timeout)
        return r.json() if r.content else None

    def user_params(self, extra=None):
        """Query params with `userId` filled in — most item endpoints are
        user-scoped (watched state, resume positions, access filtering).

        Logs in on demand: callers build the params before issuing the
        request, so the user id has to exist by then even if nothing has
        talked to the server yet.
        """
        if not self.user_id:
            self.login()
        p = {"userId": self.user_id}
        if extra:
            p.update(extra)
        return p

    # ── Items ───────────────────────────────────────────────────────────────
    def items(self, params=None):
        """GET /Items — returns (list_of_items, total_record_count)."""
        data = self.get_json("/Items", self.user_params(params)) or {}
        return data.get("Items") or [], data.get("TotalRecordCount") or 0

    def item(self, item_id, params=None):
        return self.get_json(f"/Items/{item_id}", self.user_params(params))


# ═════════════════════════════════════════════════════════════════════════════
#  Field sets
# ═════════════════════════════════════════════════════════════════════════════
# Only names from Jellyfin's ItemFields enum belong here — an unknown one is
# rejected outright.  ImageTags, UserData, RunTimeTicks, ProductionYear,
# OfficialRating, CommunityRating and the Series*/Parent* image tags all come
# back by default and must NOT be requested.
LIST_FIELDS = "Overview,Genres,ParentId,RecursiveItemCount,ChildCount,DateCreated,MediaSourceCount"

DETAIL_FIELDS = LIST_FIELDS + ",People,Studios,Taglines,Path,ProductionLocations"


# ═════════════════════════════════════════════════════════════════════════════
#  Type / sort / codec mapping
# ═════════════════════════════════════════════════════════════════════════════
TYPE_MAP = {
    "Movie":       "movie",
    "Series":      "show",
    "Season":      "season",
    "Episode":     "episode",
    "BoxSet":      "collection",
    "Playlist":    "playlist",
    "MusicArtist": "artist",
    "MusicAlbum":  "album",
    "Audio":       "track",
    "Video":       "movie",
    "Trailer":     "movie",
    "Photo":       "photo",
}

# A Jellyfin library view's CollectionType → the library type the TV UI knows
# (drives the sidebar icon and which item types a "browse all" lists).
VIEW_TYPE_MAP = {
    "movies":      "movie",
    "tvshows":     "show",
    "music":       "artist",
    "musicvideos": "movie",
    "homevideos":  "photo",
    "photos":      "photo",
    "books":       "photo",
    "boxsets":     "collection",
    "playlists":   "playlist",
}

# What "browse all" should list for each kind of library.  Without this a
# recursive listing of a TV library returns every episode instead of the shows.
VIEW_ITEM_TYPES = {
    "movies":      "Movie",
    "tvshows":     "Series",
    "music":       "MusicAlbum",
    "musicvideos": "MusicVideo",
    "homevideos":  "Video,Photo",
    "photos":      "Photo",
    "books":       "Book",
    "boxsets":     "BoxSet",
    "playlists":   "Playlist",
}

# The frontend keeps sending Plex's sort tokens; translate them here so the
# TV UI's sort menu needs no changes.
SORT_MAP = {
    "titleSort":                  ("SortName",        "Ascending"),
    "addedAt:desc":               ("DateCreated",     "Descending"),
    "originallyAvailableAt:desc": ("PremiereDate",    "Descending"),
    "year:desc":                  ("ProductionYear",  "Descending"),
    "rating:desc":                ("CommunityRating", "Descending"),
    "lastViewedAt:desc":          ("DatePlayed",      "Descending"),
}

# Jellyfin's media-segment types → the marker types the player's Skip button
# already understands.
SEGMENT_TYPE_MAP = {
    "Intro":      "intro",
    "Outro":      "credits",
    "Recap":      "recap",
    "Preview":    "preview",
    "Commercial": "commercial",
}

# Jellyfin reports image-based subtitle codecs as PGSSUB / DVDSUB / DVBSUB.
# Normalise them to the names the frontend's IMAGE_SUB_CODECS set uses, so the
# "can this be rendered client-side?" check works on both sides of the wire.
SUB_CODEC_MAP = {
    "pgssub":            "pgs",
    "hdmv_pgs_subtitle": "pgs",
    "dvdsub":            "vobsub",
    "dvd_subtitle":      "vobsub",
    "dvbsub":            "dvb_subtitle",
    "dvb_subtitle":      "dvb_subtitle",
}

IMAGE_SUB_CODECS = {"pgs", "vobsub", "dvb_subtitle"}

# Resolution buckets as (min width, min height, label), widest first.  Width
# has to lead: a 2.35:1 film is letterboxed in the *file*, so a 4K scope
# release is 3840x1632 and a 1080p one is 1920x818.  Judging by height alone
# would badge them 1440p and 720p.  Height is still checked so a stream that
# reports no width (or an anamorphic one) still lands somewhere sane.
RESOLUTION_LABELS = [
    (7600, 4300, "8K"),
    (3800, 2000, "4K"),
    (2500, 1400, "1440p"),
    (1800, 1000, "1080p"),
    (1200,  700, "720p"),
    (700,   400, "480p"),
    (400,   200, "360p"),
]


def normalize_sub_codec(codec):
    c = (codec or "").lower()
    return SUB_CODEC_MAP.get(c, c)


# Containers a browser <video> can play as a plain progressive file.  Keep in
# step with BROWSER_CONTAINERS in static/js/tv.js — the frontend decides
# Direct Play from the container this reports.
BROWSER_CONTAINERS = ("mp4", "m4v", "mov", "webm")


def normalize_container(container):
    """Reduce Jellyfin's container field to a single name.

    PlaybackInfo can answer with ffmpeg's raw demuxer list rather than a
    single container — an ordinary .mp4 comes back as
    "mov,mp4,m4a,3gp,3g2,mj2".  Passed through untouched that matches no
    known container, and the frontend downgrades a perfectly
    direct-playable file to Direct Stream.
    """
    parts = [p.strip().lower() for p in (container or "").split(",") if p.strip()]
    if not parts:
        return ""
    for candidate in BROWSER_CONTAINERS:
        if candidate in parts:
            return candidate
    return parts[0]


def resolution_label(width, height):
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return "?"
    for min_w, min_h, label in RESOLUTION_LABELS:
        if w >= min_w or h >= min_h:
            return label
    return f"{h}p" if h else "?"


def map_sort(token):
    return SORT_MAP.get(token or "", SORT_MAP["titleSort"])


# ═════════════════════════════════════════════════════════════════════════════
#  Images
# ═════════════════════════════════════════════════════════════════════════════
# The frontend treats `thumb`/`art` as opaque strings and only ever hands them
# back to /img, so they hold a real Jellyfin image path here.  The tag is
# carried along as a cache-buster: /img marks images immutable, and without a
# tag a metadata refresh would leave stale art in Chrome's cache forever.
_IMG_PATH_RE = re.compile(
    r"^/Items/[0-9a-fA-F-]{8,36}/Images/"
    r"(Primary|Backdrop|Thumb|Logo|Art|Banner|Disc|Screenshot)(/\d+)?$"
)


def image_path(item_id, kind="Primary", index=None, tag=None):
    if not item_id:
        return ""
    path = f"/Items/{item_id}/Images/{kind}"
    if index is not None:
        path += f"/{index}"
    if tag:
        path += f"?tag={tag}"
    return path


def is_safe_image_path(path):
    """Guard for /img — this proxy must only ever fetch item artwork, never
    an arbitrary path on the Jellyfin server."""
    return bool(_IMG_PATH_RE.match(path or ""))


def _first(seq):
    return seq[0] if seq else None


# ═════════════════════════════════════════════════════════════════════════════
#  Item translation
# ═════════════════════════════════════════════════════════════════════════════
def to_item(j):
    """One Jellyfin item → the flat shape the TV frontend expects."""
    if not j:
        return {}

    jtype   = j.get("Type") or ""
    item_id = j.get("Id")
    tags    = j.get("ImageTags") or {}
    ud      = j.get("UserData") or {}
    backdrops        = j.get("BackdropImageTags") or []
    parent_backdrops = j.get("ParentBackdropImageTags") or []
    series_id = j.get("SeriesId")
    season_id = j.get("SeasonId")

    item = {
        "ratingKey":     item_id,
        "key":           item_id,
        "type":          TYPE_MAP.get(jtype, jtype.lower()),
        "title":         j.get("Name") or "",
        "titleSort":     j.get("SortName") or "",
        "summary":       j.get("Overview") or "",
        "year":          j.get("ProductionYear") or "",
        "duration":      ticks_to_ms(j.get("RunTimeTicks")),
        "contentRating": j.get("OfficialRating") or "",
        "index":         j.get("IndexNumber"),
        "parentIndex":   j.get("ParentIndexNumber"),
        "viewOffset":    ticks_to_ms(ud.get("PlaybackPositionTicks")),
        "viewCount":     ud.get("PlayCount") or (1 if ud.get("Played") else 0),
        "thumb":         image_path(item_id, "Primary", tag=tags.get("Primary")),
        "art":           "",
        "mediaSourceCount": j.get("MediaSourceCount") or 0,
    }

    # ── Artwork ─────────────────────────────────────────────────────────────
    if backdrops:
        item["art"] = image_path(item_id, "Backdrop", 0, tag=backdrops[0])
    elif parent_backdrops:
        # Episodes/seasons inherit the show's backdrop.
        parent = j.get("ParentBackdropItemId") or series_id
        item["art"] = image_path(parent, "Backdrop", 0, tag=parent_backdrops[0])

    # ── Ratings ─────────────────────────────────────────────────────────────
    if j.get("CommunityRating") is not None:
        item["rating"] = round(float(j["CommunityRating"]), 1)
    if j.get("CriticRating") is not None:
        # Jellyfin's CriticRating is a Rotten Tomatoes percentage (0–100).
        item["criticRating"] = int(j["CriticRating"])

    studios = j.get("Studios") or []
    if studios:
        item["studio"] = studios[0].get("Name") or ""

    # ── Series / season lineage ─────────────────────────────────────────────
    if jtype == "Episode":
        item["grandparentTitle"]      = j.get("SeriesName") or ""
        item["grandparentRatingKey"]  = series_id
        item["parentTitle"]           = j.get("SeasonName") or ""
        item["parentRatingKey"]       = season_id
        item["grandparentThumb"]      = image_path(
            series_id, "Primary", tag=j.get("SeriesPrimaryImageTag"))
        # No tag available for the season poster; Jellyfin serves the current
        # image when the tag is omitted.
        item["parentThumb"]           = image_path(season_id, "Primary")
    elif jtype == "Season":
        item["parentTitle"]      = j.get("SeriesName") or ""
        item["parentRatingKey"]  = series_id
        item["parentThumb"]      = image_path(
            series_id, "Primary", tag=j.get("SeriesPrimaryImageTag"))

    # ── Episode counts, for the "fully watched" tick on show posters ────────
    if jtype in ("Series", "Season", "BoxSet"):
        leaf = j.get("RecursiveItemCount") or j.get("ChildCount") or 0
        item["leafCount"] = leaf
        unplayed = ud.get("UnplayedItemCount")
        if leaf and unplayed is not None:
            item["viewedLeafCount"] = max(0, leaf - unplayed)

    return item


def to_items(seq):
    return [to_item(j) for j in (seq or [])]


def to_detail(j):
    """Full metadata for the detail screen — adds genres, cast and crew."""
    item = to_item(j)
    if not item:
        return item

    item["Genre"] = [{"tag": g} for g in (j.get("Genres") or [])]

    people = j.get("People") or []
    for role, key in (("Director", "Director"), ("Writer", "Writer"), ("Actor", "Role")):
        item[key] = [{"tag": p.get("Name") or ""} for p in people
                     if p.get("Type") == role and p.get("Name")]

    taglines = j.get("Taglines") or []
    if taglines:
        item["tagline"] = taglines[0]

    return item


def to_library(view):
    """A Jellyfin user view → a library entry for the sidebar / home rows."""
    ctype = (view.get("CollectionType") or "").lower()
    return {
        "key":            view.get("Id"),
        "title":          view.get("Name") or "",
        "type":           VIEW_TYPE_MAP.get(ctype, "movie"),
        "collectionType": ctype,
    }


def to_markers(segments, runtime_ms=0):
    """Jellyfin media segments → the intro/credits markers the player's Skip
    button consumes.  A segment that runs to the end of the file is flagged
    `final`, which makes the player jump to the end (triggering Up Next)
    instead of landing on the last frame."""
    out = []
    for seg in segments or []:
        mtype = SEGMENT_TYPE_MAP.get(seg.get("Type"))
        if not mtype:
            continue
        start = ticks_to_ms(seg.get("StartTicks"))
        end   = ticks_to_ms(seg.get("EndTicks"))
        if end <= start:
            continue
        out.append({
            "type":  mtype,
            "start": start,
            "end":   end,
            "final": bool(runtime_ms and end >= runtime_ms - 2000),
        })
    out.sort(key=lambda m: m["start"])
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Media sources & streams
# ═════════════════════════════════════════════════════════════════════════════
def video_stream(source):
    for s in source.get("MediaStreams") or []:
        if s.get("Type") == "Video":
            return s
    return {}


def source_rank(source):
    """Sort key for picking the default version of an item.

    Jellyfin returns every file it found for a title, in no particular order —
    a 1080p mp4 can come before a 4K remux.  Real clients pick deliberately;
    here "best" means tallest picture, then highest bitrate.
    """
    v = video_stream(source)
    return (int(v.get("Height") or 0), int(source.get("Bitrate") or 0))


def pick_source(sources, media_source_id=None):
    """Choose which file to play: the one asked for, else the best available."""
    sources = [s for s in (sources or []) if s]
    if not sources:
        return None
    if media_source_id:
        for s in sources:
            if s.get("Id") == media_source_id:
                return s
    return max(sources, key=source_rank)


def source_label(source):
    """Short human label for a version, e.g. "4K HEVC · MKV · 15.6 Mbps"."""
    v = video_stream(source)
    bits = [resolution_label(v.get("Width"), v.get("Height")),
            (v.get("Codec") or "").upper(),
            normalize_container(source.get("Container")).upper()]
    bitrate = source.get("Bitrate") or 0
    if bitrate:
        bits.append(f"{bitrate / 1_000_000:.1f} Mbps")
    return " · ".join(b for b in bits if b)


def source_quality(source):
    """The badge the player shows: resolution, codecs, container, bitrate."""
    v = video_stream(source)
    audio = next((s for s in source.get("MediaStreams") or []
                  if s.get("Type") == "Audio"), {})
    return {
        "videoCodec":  (v.get("Codec") or "?").upper(),
        "audioCodec":  (audio.get("Codec") or "?").upper(),
        # HDR / SDR. The player needs this to tell a genuine copy variant from
        # a tone-mapped one when it picks an HLS rung.
        "videoRange":  (v.get("VideoRange") or "").upper(),
        "resolution":  resolution_label(v.get("Width"), v.get("Height")),
        "width":       v.get("Width") or 0,
        "height":      v.get("Height") or 0,
        "bitrateKbps": int((source.get("Bitrate") or 0) // 1000),
        "container":   normalize_container(source.get("Container")).upper() or "?",
    }


def source_bitrate(source):
    """Best available bitrate for a source, in bits per second."""
    try:
        bitrate = int(source.get("Bitrate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    if bitrate:
        return bitrate
    v = video_stream(source)
    try:
        return int(v.get("BitRate") or 0)
    except (TypeError, ValueError):
        return 0


def track_lists(source, audio_index=None, sub_index=None):
    """Split a source's MediaStreams into the audio / subtitle pickers.

    Track ids are **stream index + 1**.  Jellyfin's stream indexes start at 0
    and the frontend treats a falsy track id as the synthetic "Off" entry, so
    a real subtitle at index 0 would silently read as "no subtitles".  The
    offset is undone in `stream_index()`.
    """
    audio_tracks = []
    sub_tracks   = [{"id": 0, "title": "Off", "language": "", "codec": "",
                     "forced": False, "external": False, "selected": False}]

    if audio_index is None:
        audio_index = source.get("DefaultAudioStreamIndex")
    if sub_index is None:
        sub_index = source.get("DefaultSubtitleStreamIndex")

    for s in source.get("MediaStreams") or []:
        idx   = s.get("Index")
        stype = s.get("Type")
        title = s.get("DisplayTitle") or s.get("Title") or s.get("Language") or ""

        if stype == "Audio":
            audio_tracks.append({
                "id":       (idx or 0) + 1,
                "title":    title or "Audio",
                "language": s.get("Language") or "",
                "channels": s.get("Channels") or 0,
                "codec":    (s.get("Codec") or "").lower(),
                "selected": idx == audio_index,
            })
        elif stype == "Subtitle":
            codec = normalize_sub_codec(s.get("Codec"))
            sub_tracks.append({
                "id":       (idx or 0) + 1,
                "title":    title or "Subtitle",
                "language": s.get("Language") or "",
                "codec":    codec,
                "forced":   bool(s.get("IsForced")),
                "external": bool(s.get("IsExternal")),
                "text":     bool(s.get("IsTextSubtitleStream")),
                "selected": idx == sub_index,
            })

    if not any(t["selected"] for t in audio_tracks) and audio_tracks:
        audio_tracks[0]["selected"] = True

    # Jellyfin happily marks an embedded PGS track as the default subtitle.
    # Honouring that would burn subs into a transcode the moment playback
    # starts, so an image-based default is downgraded to "Off" — the user can
    # still pick it explicitly from the subtitle menu.
    selected_sub = next((t for t in sub_tracks if t["selected"]), None)
    if selected_sub and selected_sub["codec"] in IMAGE_SUB_CODECS:
        selected_sub["selected"] = False
        selected_sub = None
    if not selected_sub:
        sub_tracks[0]["selected"] = True

    return audio_tracks, sub_tracks


def stream_index(track_id):
    """Track id (1-based, 0 = off) → Jellyfin stream index, or None for off."""
    try:
        tid = int(track_id)
    except (TypeError, ValueError):
        return None
    return tid - 1 if tid > 0 else None


def find_stream(source, index):
    for s in source.get("MediaStreams") or []:
        if s.get("Index") == index:
            return s
    return None
