#!/usr/bin/env python3
"""
twitch_m3u — turn Twitch channels into an M3U playlist any HLS player can open.

Twitch does not publish stable .m3u8 URLs. A playable URL has to be minted per
request: ask the public GQL endpoint for a signed PlaybackAccessToken, then hand
that token to usher.ttvnw.net, which returns an HLS master playlist. Those URLs
are short-lived, so a playlist with URLs baked into it goes stale.

So there are two playlist flavours here:

  serve   run a tiny local redirect server; playlist entries point at it and it
          mints a fresh URL on every open. Entries never expire.  <- recommended
  build --direct
          bake current usher URLs straight into the file. No server needed, but
          only works for channels live right now, and only for a while.

Usage:
  ./twitch_m3u.py resolve <channel> [-q 720p60]     print a playable URL
  ./twitch_m3u.py serve [--port 7777]               start the redirect server
  ./twitch_m3u.py build [-o twitch.m3u]             playlist -> local server
  ./twitch_m3u.py build --direct [-o twitch.m3u]    playlist -> baked URLs
  ./twitch_m3u.py status                            who in channels.txt is live
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hmac
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Public client-id used by the Twitch web player for anonymous playback.
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_URL = "https://gql.twitch.tv/gql"
USHER_LIVE = "https://usher.ttvnw.net/api/channel/hls/{channel}.m3u8"
USHER_VOD = "https://usher.ttvnw.net/vod/{vod_id}.m3u8"
ACCESS_TOKEN_HASH = (
    "0828119ded1c13477966434e15800ff57ddacf13ba1911c129dc2200705b0712"
)
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS_FILE = os.path.join(HERE, "channels.txt")


class TwitchError(RuntimeError):
    pass


class Offline(TwitchError):
    """Channel exists (or not) but has no live HLS manifest right now."""


# ---------------------------------------------------------------- http helpers

def _post_gql(payload: dict, timeout: float = 10.0) -> dict:
    headers = {
        "Client-ID": CLIENT_ID,
        "Content-Type": "application/json",
        "User-Agent": UA,
        "Origin": "https://www.twitch.tv",
        "Referer": "https://www.twitch.tv/",
    }
    # Optional: your own account's token, read from the environment and never
    # logged. With Turbo or a channel sub, Twitch itself issues an ad-free
    # playback token (the token carries hide_ads / turbo / subscriber flags),
    # which also removes the ad discontinuity that stalls players.
    auth = os.environ.get("TWITCH_AUTH_TOKEN", "").strip()
    if auth:
        headers["Authorization"] = "OAuth " + auth.removeprefix("oauth:")
    req = urllib.request.Request(
        GQL_URL, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------------- resolving

def playback_token(channel: str = "", vod_id: str = "") -> tuple[str, str]:
    """Return (token, signature) for a live channel or a VOD."""
    data = _post_gql({
        "operationName": "PlaybackAccessToken",
        "extensions": {"persistedQuery": {
            "version": 1, "sha256Hash": ACCESS_TOKEN_HASH}},
        "variables": {
            "isLive": not vod_id,
            "login": channel.lower(),
            "isVod": bool(vod_id),
            "vodID": vod_id,
            "playerType": "site",
        },
    })
    if data.get("errors"):
        raise TwitchError(data["errors"][0].get("message", "GQL error"))
    key = "videoPlaybackAccessToken" if vod_id else "streamPlaybackAccessToken"
    tok = (data.get("data") or {}).get(key)
    if not tok:
        raise Offline(f"no playback token for {vod_id or channel}")
    return tok["value"], tok["signature"]


_SESSION_IDS: dict[str, tuple[str, str]] = {}
_SESSION_LOCK = threading.Lock()


def session_ids(key: str) -> tuple[str, str]:
    """Stable (device_id, play_session_id) per channel.

    Twitch serves a PREROLL whenever a *new* playback session starts. Minting
    fresh ids on every open means a player that stalls and retries earns
    another 30s ad, forever. Reusing them makes a reconnect a continuation.
    """
    with _SESSION_LOCK:
        if key not in _SESSION_IDS:
            _SESSION_IDS[key] = ("%016x" % random.getrandbits(64),
                                 "%032x" % random.getrandbits(128))
        return _SESSION_IDS[key]


def _usher(base: str, token: str, sig: str, session_key: str = "") -> str:
    device_id, play_session = session_ids(session_key or base)
    params = {
        "sig": sig,
        "token": token,
        "allow_source": "true",
        "allow_audio_only": "true",
        "fast_bread": "true",           # low-latency segment delivery
        "p": str(random.randint(1_000_000, 9_999_999)),
        "play_session_id": play_session,
        "device_id": device_id,
        "player_backend": "mediaplayer",
        "playlist_include_framerate": "true",
        "reassignments_supported": "true",
        "supported_codecs": "avc1",     # h264 only: widest player support
        "transcode_mode": "cbr_v1",
        "cdm": "wv",
        "player_version": "1.32.0",
    }
    return base + "?" + urllib.parse.urlencode(params)


def master_playlist(channel: str = "", vod_id: str = "") -> tuple[str, str]:
    """Return (master_url, master_body). Raises Offline if not streaming."""
    token, sig = playback_token(channel, vod_id)
    base = (USHER_VOD.format(vod_id=vod_id) if vod_id
            else USHER_LIVE.format(channel=channel.lower()))
    url = _usher(base, token, sig, session_key=(vod_id or channel).lower())
    try:
        body = _get(url)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise Offline(f"{vod_id or channel} is offline or unavailable")
        raise
    if not body.lstrip().startswith("#EXTM3U"):
        raise Offline(f"{vod_id or channel} returned no manifest")
    return url, body


_VARIANT = re.compile(
    r'#EXT-X-STREAM-INF:(?P<attrs>[^\n]*)\n(?P<url>https?://[^\s]+)')


def variants(master_body: str) -> list[dict]:
    """Parse a master playlist into [{name, group, resolution, url, ...}]."""
    groups = dict(re.findall(
        r'#EXT-X-MEDIA:[^\n]*GROUP-ID="([^"]+)"[^\n]*NAME="([^"]+)"',
        master_body))
    out = []
    for m in _VARIANT.finditer(master_body):
        attrs = dict(re.findall(r'([A-Z-]+)=("[^"]*"|[^,]*)', m.group("attrs")))
        group = attrs.get("VIDEO", "").strip('"')
        out.append({
            "group": group,
            "name": groups.get(group, group),
            "resolution": attrs.get("RESOLUTION", ""),
            "bandwidth": int(attrs.get("BANDWIDTH", "0") or 0),
            "framerate": attrs.get("FRAME-RATE", ""),
            "url": m.group("url"),
        })
    return out


def pick_variant(master_body: str, quality: str) -> str | None:
    """quality: best | worst | audio | audio_only | 720p60 | 480p | 1080 ..."""
    vs = variants(master_body)
    if not vs:
        return None
    q = (quality or "best").strip().lower()
    if q in ("best", "source", "chunked"):
        return max(vs, key=lambda v: v["bandwidth"])["url"]
    if q in ("audio", "audio_only", "audioonly"):
        for v in vs:
            if "audio" in v["group"].lower():
                return v["url"]
        return min(vs, key=lambda v: v["bandwidth"])["url"]
    if q == "worst":
        video = [v for v in vs if "audio" not in v["group"].lower()] or vs
        return min(video, key=lambda v: v["bandwidth"])["url"]
    for v in vs:                                    # exact: "720p60"
        if q in (v["group"].lower(), v["name"].lower()):
            return v["url"]
    height = re.match(r"(\d{3,4})", q)              # loose: "720", "1080p"
    if height:
        want = int(height.group(1))
        cands = [v for v in vs if v["resolution"].endswith("x" + str(want))]
        if cands:
            return max(cands, key=lambda v: v["bandwidth"])["url"]
        video = [v for v in vs if "x" in v["resolution"]]
        if video:                                   # nearest at or below
            below = [v for v in video
                     if int(v["resolution"].split("x")[1]) <= want]
            pool = below or video
            return max(pool, key=lambda v: v["bandwidth"])["url"]
    return None


def resolve(channel: str = "", quality: str = "best", vod_id: str = "") -> str:
    """Channel/VOD -> a directly playable HLS URL."""
    url, body = master_playlist(channel, vod_id)
    if (quality or "best").lower() in ("master", "multi", "abr"):
        return url                       # let the player do the ABR switching
    return pick_variant(body, quality) or url


# -------------------------------------------------------------------- metadata

def channel_info(logins: list[str]) -> dict[str, dict]:
    """Batch live status + title + game + avatar for up to ~100 logins."""
    info: dict[str, dict] = {}
    logins = [c.lower() for c in logins]
    for i in range(0, len(logins), 50):
        chunk = logins[i:i + 50]
        data = _post_gql({
            "query": "query($logins:[String!]){users(logins:$logins){"
                     "login displayName profileImageURL(width:150) "
                     "stream{id title type viewersCount createdAt "
                     "game{name}}}}",
            "variables": {"logins": chunk},
        })
        for u in (data.get("data") or {}).get("users") or []:
            if not u:
                continue
            st = u.get("stream") or {}
            info[u["login"].lower()] = {
                "login": u["login"],
                "display": u.get("displayName") or u["login"],
                "logo": u.get("profileImageURL") or "",
                "live": bool(st),
                "title": (st.get("title") or "").strip(),
                "game": ((st.get("game") or {}) or {}).get("name") or "",
                "viewers": st.get("viewersCount") or 0,
                "started": st.get("createdAt") or "",
            }
    for c in logins:
        info.setdefault(c, {"login": c, "display": c, "logo": "", "live": False,
                            "title": "", "game": "", "viewers": 0,
                            "started": ""})
    return info


# ----------------------------------------------------------------- ad-stall fix
# During a Twitch ad the media playlist behaves like a VOD: MEDIA-SEQUENCE
# pins to 0 and the window grows, then snaps forward when content resumes.
# Measured on a live preroll: seq 0 for ~60s across a 4->30 segment window,
# then seq 18 with a 15 segment window. Players that assume a monotonic
# sequence treat that as fatal and sit on "commercial break in progress"
# forever, even though the stream itself has already recovered.
#
# This rewrites the manifest so the sequence only ever moves forward. The ad
# still plays; the player simply survives it and returns to content.

class _SeqNormaliser:
    """Assign monotonically increasing sequence numbers to segment URIs."""

    KEEP = 4000

    def __init__(self):
        self.index: dict[str, int] = {}
        self.order: list[str] = []
        self.next = 0
        self.lock = threading.Lock()

    def rewrite(self, body: str) -> str:
        lines = body.splitlines()
        with self.lock:
            first = None
            for ln in lines:
                if not ln or ln.startswith("#"):
                    continue
                if ln not in self.index:
                    self.index[ln] = self.next
                    self.order.append(ln)
                    self.next += 1
                if first is None:
                    first = self.index[ln]
            if len(self.order) > self.KEEP:          # bound memory
                for old in self.order[:-self.KEEP]:
                    self.index.pop(old, None)
                self.order = self.order[-self.KEEP:]
            seq = 0 if first is None else first

        out, seen_seq = [], False
        for ln in lines:
            if ln.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                out.append(f"#EXT-X-MEDIA-SEQUENCE:{seq}")
                seen_seq = True
            elif ln.startswith("#EXT-X-TWITCH-LIVE-SEQUENCE:"):
                continue                              # confuses some players
            else:
                out.append(ln)
        if not seen_seq:
            for i, ln in enumerate(out):
                if ln.startswith("#EXT-X-VERSION"):
                    out.insert(i + 1, f"#EXT-X-MEDIA-SEQUENCE:{seq}")
                    break
        return "\n".join(out) + "\n"


_NORMALISERS: dict[str, _SeqNormaliser] = {}
_NORM_LOCK = threading.Lock()


def normaliser(key: str) -> _SeqNormaliser:
    with _NORM_LOCK:
        if key not in _NORMALISERS:
            _NORMALISERS[key] = _SeqNormaliser()
        return _NORMALISERS[key]


def in_ad_break(body: str) -> bool:
    return "twitch-stitched-ad" in body


# ------------------------------------------------------------------- discovery
# Twitch caps a directory page at 30 and gates deeper paging (`after:` cursors)
# behind an integrity challenge, so we never page. To get breadth we fan out
# across categories instead — each is its own un-gated first page of 30.

# Per-field caps, measured against the live API. They differ, and getting
# these wrong is what kept the playlist small.
TOP_MAX = 30           # global directory page; above this returns nothing
GAMES_MAX = 100        # categories per request
GAME_STREAMS_MAX = 100 # streams within one category
SEARCH_MAX = 100       # categories per search term
_STREAM_FIELDS = """
  title viewersCount createdAt
  broadcaster { login displayName profileImageURL(width:150) }
  game { name }
"""


def _meta_from_node(node: dict) -> dict | None:
    b = (node or {}).get("broadcaster") or {}
    if not b.get("login"):
        return None
    return {
        "login": b["login"].lower(),
        "display": b.get("displayName") or b["login"],
        "logo": b.get("profileImageURL") or "",
        "live": True,
        "title": (node.get("title") or "").strip(),
        "game": ((node.get("game") or {}) or {}).get("name") or "",
        "viewers": node.get("viewersCount") or 0,
        "started": node.get("createdAt") or "",
    }


def _clamp(n: int, cap: int) -> int:
    return min(max(int(n), 1), cap)


def _edges(data: dict, *path: str) -> list:
    node = data.get("data") or {}
    for key in path:
        node = (node or {}).get(key) or {}
    return (node or {}).get("edges") or []


def top_streams(limit: int = 30, language: str = "") -> list[dict]:
    """The global directory front page (capped at PAGE_MAX)."""
    opts = "first:" + str(_clamp(limit, TOP_MAX))
    if language:
        opts += ",options:{languages:[" + language.upper() + "]}"
    q = "query{streams(" + opts + "){edges{node{" + _STREAM_FIELDS + "}}}}"
    data = _post_gql({"query": q})
    if data.get("errors"):
        raise TwitchError(data["errors"][0].get("message", "GQL error"))
    metas = [_meta_from_node(e.get("node")) for e in _edges(data, "streams")]
    return [m for m in metas if m][:limit]


def top_games(limit: int = 10) -> list[str]:
    q = "query{games(first:" + str(_clamp(limit, GAMES_MAX)) + "){edges{node{name}}}}"
    data = _post_gql({"query": q})
    return [e["node"]["name"] for e in _edges(data, "games")
            if (e.get("node") or {}).get("name")][:limit]


def search_categories(term: str, limit: int = SEARCH_MAX) -> list[str]:
    """Category names matching a search term (100 per term)."""
    q = ('query($t:String!){searchCategories(query:$t,first:'
         + str(_clamp(limit, SEARCH_MAX)) + '){edges{node{name}}}}')
    data = _post_gql({"query": q, "variables": {"term": term}} if False else
                     {"query": q, "variables": {"t": term}})
    return [e["node"]["name"] for e in _edges(data, "searchCategories")
            if (e.get("node") or {}).get("name")]


def all_categories(games: int = GAMES_MAX, deep: bool = False,
                   workers: int = 8) -> list[str]:
    """Top categories, optionally widened by searching the alphabet."""
    names, seen = [], set()

    def add(batch):
        for n in batch:
            if n.lower() not in seen:
                seen.add(n.lower())
                names.append(n)

    add(top_games(games))
    if deep:
        terms = list("abcdefghijklmnopqrstuvwxyz0123456789")
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in pool.map(
                    lambda t_: _safe(search_categories, t_), terms):
                add(batch)
    return names


def _safe(fn, *a):
    try:
        return fn(*a) or []
    except Exception:                       # noqa: BLE001 - breadth, not depth
        return []


def game_streams(game: str, limit: int = 30) -> list[dict]:
    q = ("query($name:String!){game(name:$name){streams(first:"
         + str(_clamp(limit, GAME_STREAMS_MAX))
         + "){edges{node{" + _STREAM_FIELDS + "}}}}}")
    data = _post_gql({"query": q, "variables": {"name": game}})
    if data.get("errors"):
        raise TwitchError(data["errors"][0].get("message", "GQL error"))
    metas = [_meta_from_node(e.get("node"))
             for e in _edges(data, "game", "streams")]
    return [m for m in metas if m][:limit]


_DISCOVER_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_DISCOVER_LOCK = threading.Lock()
DISCOVER_TTL = 120.0


_WARM: dict[tuple, dict] = {}      # param-sets a client has actually asked for
LAST_REFRESH: dict[str, float] = {}


def _cache_key(kw: dict) -> tuple:
    return tuple(sorted((k, v) for k, v in kw.items() if k != "progress"))


def _store(key: tuple, metas: list[dict], ttl: float) -> None:
    with _DISCOVER_LOCK:
        _DISCOVER_CACHE[key] = (time.time() + ttl, metas)
        if len(_DISCOVER_CACHE) > 16:
            stale = sorted(_DISCOVER_CACHE,
                           key=lambda k: _DISCOVER_CACHE[k][0])[:8]
            for k in stale:
                _DISCOVER_CACHE.pop(k, None)


def discover_cached(**kw) -> list[dict]:
    """discover() memoised, so a playlist and its EPG agree — and so a
    background refresh can keep the answer warm instead of making a client
    wait for a full scan."""
    key = _cache_key(kw)
    with _DISCOVER_LOCK:
        _WARM[key] = dict(kw)
        hit = _DISCOVER_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    metas = discover(**kw)
    _store(key, metas, DISCOVER_TTL)
    LAST_REFRESH[str(key)] = time.time()
    return metas


def refresh_loop(interval: float, stop: threading.Event) -> None:
    """Re-run every param-set a client has asked for, on a timer.

    Viewer counts and live/offline status go stale within minutes. Refreshing
    in the background means a client fetch is always both current and instant,
    even for a deep scan that takes the better part of a minute to build.
    """
    while not stop.wait(interval):
        with _DISCOVER_LOCK:
            warm = list(_WARM.items())
        for key, kw in warm:
            try:
                started = time.time()
                metas = discover(**kw)
                _store(key, metas, max(DISCOVER_TTL, interval * 3))
                LAST_REFRESH[str(key)] = time.time()
                print(f"  refreshed {len(metas)} channels in "
                      f"{time.time() - started:.1f}s", file=sys.stderr)
            except Exception as e:                # noqa: BLE001 keep looping
                print(f"  refresh failed: {type(e).__name__}: {e}",
                      file=sys.stderr)


def sort_metas(metas: list[dict], how: str = "viewers") -> list[dict]:
    """Twitch returns categories in its own 'recommended' order, not by
    audience, so a playlist looks shuffled unless we sort it ourselves."""
    how = (how or "viewers").lower()
    if how in ("none", "off", "api"):
        return metas
    if how in ("name", "alpha"):
        return sorted(metas, key=lambda m: m["display"].lower())
    if how in ("asc", "viewers_asc", "smallest"):
        return sorted(metas, key=lambda m: (m.get("viewers") or 0))
    # default: biggest audience first, ties broken by name for stability
    return sorted(metas, key=lambda m: (-(m.get("viewers") or 0),
                                        m["display"].lower()))


def discover(games: int = 0, per_game: int = 20, top: int = 30,
             language: str = "", deep: bool = False, workers: int = 8,
             progress: bool = False, sort: str = "viewers") -> list[dict]:
    """Top streams plus the streams of many categories, fetched in parallel."""
    seen: set[str] = set()
    out: list[dict] = []

    def add(metas, group):
        for m in metas:
            if m["login"] in seen:
                continue
            seen.add(m["login"])
            m["group"] = group
            out.append(m)

    if top:
        add(_safe(top_streams, top, language), "Top Live")

    cats = all_categories(games, deep=deep, workers=workers) if games else []
    if cats and progress:
        print(f"  scanning {len(cats)} categories…", file=sys.stderr)
    if cats:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_safe, game_streams, c, per_game): c
                    for c in cats}
            done = 0
            for fut in cf.as_completed(futs):
                add(fut.result(), futs[fut])
                done += 1
                if progress and done % 25 == 0:
                    print(f"  {done}/{len(cats)} categories, "
                          f"{len(out)} channels", file=sys.stderr)
    return sort_metas(out, sort)


# ------------------------------------------------------------------------ epg
# IPTV apps show "No information available" unless an XMLTV guide is attached.
# Each live stream becomes one programme: title, category, viewer count, and a
# real start time from the stream's createdAt.

def _xml_escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _xmltv_time(iso: str, fallback: float | None = None) -> str:
    """'2026-08-17T17:02:26Z' -> '20260817170226 +0000' (XMLTV format)."""
    try:
        stamp = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        stamp = dt.datetime.fromtimestamp(fallback or time.time(),
                                          dt.timezone.utc)
    return stamp.strftime("%Y%m%d%H%M%S +0000")


def build_epg(metas: list[dict], hours: int = 12) -> str:
    """XMLTV for the same channels the playlist carries."""
    now = time.time()
    stop = dt.datetime.fromtimestamp(now + hours * 3600, dt.timezone.utc)
    stop_s = stop.strftime("%Y%m%d%H%M%S +0000")

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="twitch_m3u">']
    for m in metas:
        cid = _xml_escape(m["login"])
        out.append(f'  <channel id="{cid}">')
        out.append(f'    <display-name>{_xml_escape(m["display"])}'
                   f'</display-name>')
        if m.get("logo"):
            out.append(f'    <icon src="{_xml_escape(m["logo"])}" />')
        out.append("  </channel>")

    for m in metas:
        if not m.get("live"):
            continue
        cid = _xml_escape(m["login"])
        start = _xmltv_time(m.get("started", ""), now)
        title = m.get("title") or m["display"]
        desc = []
        if m.get("game"):
            desc.append(m["game"])
        if m.get("viewers"):
            desc.append(f"{m['viewers']:,} viewers")
        out.append(f'  <programme start="{start}" stop="{stop_s}" '
                   f'channel="{cid}">')
        out.append(f'    <title lang="en">{_xml_escape(title)}</title>')
        if desc:
            out.append(f'    <desc lang="en">{_xml_escape(" · ".join(desc))}'
                       f'</desc>')
        if m.get("game"):
            out.append(f'    <category lang="en">{_xml_escape(m["game"])}'
                       f'</category>')
        if m.get("logo"):
            out.append(f'    <icon src="{_xml_escape(m["logo"])}" />')
        out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"


# -------------------------------------------------------------------- playlist

def read_channels(path: str = CHANNELS_FILE) -> list[str]:
    if not os.path.exists(path):
        raise SystemExit(f"no channel list at {path} — create it, one login "
                         f"per line (# comments allowed)")
    out, seen = [], set()
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # accept bare logins and full twitch.tv URLs
        line = re.sub(r"^https?://(www\.)?twitch\.tv/", "", line).strip("/")
        login = line.lower()
        if login and login not in seen:
            seen.add(login)
            out.append(login)
    return out


def human(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _extinf(meta: dict, group_title: str) -> str:
    name = meta["display"]
    if meta["live"]:
        bits = []
        if meta.get("viewers"):
            bits.append(f"{human(meta['viewers'])} viewers")
        if meta.get("game"):
            bits.append(meta["game"])
        if bits:
            name = f"{name} · " + " · ".join(bits)
    else:
        name = f"{name} (offline)"
    return (f'#EXTINF:-1 tvg-id="{meta["login"]}" tvg-name="{meta["display"]}" '
            f'tvg-logo="{meta["logo"]}" group-title="{group_title}",{name}')


def playlist_from_meta(metas: list[dict], *, direct: bool, quality: str,
                       base: str = "", host: str = "", port: int = 0,
                       proxy: bool = False, epg_url: str = "",
                       key: str = "") -> str:
    """Render already-fetched channel metadata as an M3U."""
    base = base or f"http://{host or '127.0.0.1'}:{port or 7777}"
    lines = [f'#EXTM3U x-tvg-url="{epg_url}"']
    for meta in metas:
        if direct:
            try:
                url = resolve(meta["login"], quality)
            except Offline:
                continue
            except TwitchError as e:
                print(f"  ! {meta['login']}: {e}", file=sys.stderr)
                continue
        else:
            route = "hls" if proxy else "live"
            url = (f"{base}/{route}/{meta['login']}.m3u8"
                   f"?q={urllib.parse.quote(quality)}")
            if key:
                url += f"&key={urllib.parse.quote(key)}"
        lines.append(_extinf(meta, meta.get("group") or "Twitch"))
        lines.append(url)
    return "\n".join(lines) + "\n"


def build_playlist(channels: list[str], *, direct: bool, host: str, port: int,
                   quality: str, live_only: bool) -> str:
    info = channel_info(channels)
    lines = ['#EXTM3U x-tvg-url=""']
    for ch in channels:
        meta = info[ch]
        if live_only and not meta["live"]:
            continue
        if direct:
            try:
                url = resolve(ch, quality)
            except Offline:
                continue
            except TwitchError as e:
                print(f"  ! {ch}: {e}", file=sys.stderr)
                continue
        else:
            url = (f"http://{host}:{port}/live/{ch}.m3u8"
                   f"?q={urllib.parse.quote(quality)}")
        lines.append(_extinf(meta, "Twitch"))
        lines.append(url)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- server

class _Cache:
    """Short TTL cache so a player re-opening a stream doesn't re-hit GQL."""

    def __init__(self, ttl: float = 600.0):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        return None

    def put(self, key, val):
        with self._lock:
            self._d[key] = (time.time() + self.ttl, val)


def _int(qs: dict, key: str, default: int) -> int:
    try:
        return max(0, int((qs.get(key) or [default])[0]))
    except (TypeError, ValueError):
        return default


def _flag(qs: dict, key: str, default: bool = False) -> bool:
    """?flag, ?flag=1, ?flag=true -> True;  ?flag=0/false -> False."""
    if key not in qs:
        return default
    return (qs[key] or [""])[0].lower() in ("", "1", "true", "yes")


class Handler(BaseHTTPRequestHandler):
    server_version = "twitch_m3u"
    cache = _Cache()          # long TTL: re-resolving = another preroll
    channels_path = CHANNELS_FILE
    default_quality = "best"
    proxy_default = True      # playlists point at /hls (ad-stall-proof)
    access_key = ""           # when set, every endpoint requires ?key=

    def log_message(self, fmt, *a):     # one tidy line per request
        sys.stderr.write("  %s\n" % (fmt % a))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        quality = (qs.get("q") or qs.get("quality")
                   or [self.default_quality])[0]
        path = u.path

        if self.access_key:
            supplied = (qs.get("key") or [""])[0]
            if not hmac.compare_digest(supplied, self.access_key):
                self.send_error(403, "missing or bad ?key=")
                return

        try:
            # A player pointed at the bare host must get a playlist, not prose:
            # IPTV apps happily parse a help page into one junk channel a line.
            wants_html = "text/html" in (self.headers.get("Accept") or "")
            if path in ("/", "/index.html", "/help") and (
                    wants_html or path == "/help"):
                return self._text(self._help())

            if path in ("/", "/index.html", "/playlist.m3u8", "/playlist.m3u",
                        "/playlist", "/live.m3u8", "/index.m3u", "/index.m3u8"):
                metas = []
                if _flag(qs, "mine", default=True):
                    chans = read_channels(self.channels_path)
                    info = channel_info(chans)
                    want_all = _flag(qs, "all", default=False)
                    metas = [dict(info[c], group="Twitch") for c in chans
                             if want_all or info[c]["live"]]
                metas += self._discovered(qs)
                return self._m3u(self._dedupe(metas), quality,
                                 self._epg_url(path, qs))

            if path in ("/epg.xml", "/epg", "/guide.xml", "/xmltv.xml"):
                src = (qs.get("src") or ["playlist"])[0]
                src_path = "/" + src.lstrip("/")
                metas = self._channel_set(src_path, qs)
                return self._text(build_epg(metas, _int(qs, "hours", 12)),
                                  "application/xml; charset=utf-8")

            if path in ("/top.m3u8", "/top.m3u", "/top"):
                metas = top_streams(_int(qs, "n", 30),
                                    (qs.get("lang") or [""])[0])
                for meta in metas:
                    meta["group"] = "Top Live"
                return self._m3u(sort_metas(metas, (qs.get("sort")
                                 or ["viewers"])[0]), quality,
                                 self._epg_url(path, qs))

            if path in ("/games.m3u8", "/games.m3u", "/games", "/all.m3u8"):
                return self._m3u(discover_cached(
                    games=_int(qs, "games", 100), per_game=_int(qs, "per", 100),
                    top=_int(qs, "top", 30),
                    language=(qs.get("lang") or [""])[0],
                    deep=_flag(qs, "deep"),
                    workers=_int(qs, "workers", 8),
                    sort=(qs.get("sort") or ["viewers"])[0]), quality,
                    self._epg_url(path, qs))

            m = re.fullmatch(r"/game/(.+?)(?:\.m3u8?)?", path)
            if m:
                name = urllib.parse.unquote(m.group(1))
                metas = game_streams(name, _int(qs, "n", 100))
                for meta in metas:
                    meta["group"] = name
                return self._m3u(sort_metas(metas, (qs.get("sort")
                                 or ["viewers"])[0]), quality,
                                 self._epg_url(path, qs))

            m = re.fullmatch(r"/hls/([A-Za-z0-9_]{2,30})(?:\.m3u8?)?", path)
            if m:
                return self._proxy_media(m.group(1).lower(), quality)

            m = re.fullmatch(r"/live/([A-Za-z0-9_]{2,30})(?:\.m3u8?)?", path)
            if m:
                return self._redirect_stream(m.group(1).lower(), quality)

            m = re.fullmatch(r"/vod/(\d+)(?:\.m3u8?)?", path)
            if m:
                return self._redirect_stream("", quality, vod=m.group(1))

            self.send_error(404, "try /playlist.m3u8, /live/<channel>.m3u8, or /help")
        except BrokenPipeError:
            pass                            # player closed the connection
        except Offline as e:
            self.send_error(404, str(e))
        except Exception as e:              # noqa: BLE001 - report, don't die
            self.send_error(502, f"{type(e).__name__}: {e}")

    def _redirect_stream(self, channel: str, quality: str, vod: str = ""):
        key = (channel or f"vod:{vod}", quality)
        url = self.cache.get(key)
        if not url:
            url = resolve(channel, quality, vod_id=vod)
            self.cache.put(key, url)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _proxy_media(self, channel: str, quality: str):
        """Serve the media playlist with a monotonic sequence, so a player
        rides through an ad break instead of stalling on it forever."""
        key = (channel, quality)
        url = self.cache.get(key)
        if not url:
            url = resolve(channel, quality)
            self.cache.put(key, url)
        try:
            body = _get(url)
        except urllib.error.HTTPError:
            self.cache.put(key, None)          # stale variant: re-resolve once
            url = resolve(channel, quality)
            self.cache.put(key, url)
            body = _get(url)
        fixed = normaliser(f"{channel}/{quality}").rewrite(body)
        if in_ad_break(body):
            self.log_message("%s: ad break, riding through", channel)
        self._text(fixed, "application/vnd.apple.mpegurl")

    def _channel_set(self, path: str, qs: dict) -> list[dict]:
        """The exact channels a given playlist path represents."""
        how = (qs.get("sort") or ["viewers"])[0]
        if path.startswith("/top"):
            metas = top_streams(_int(qs, "n", 30),
                                (qs.get("lang") or [""])[0])
            for m in metas:
                m["group"] = "Top Live"
            return sort_metas(metas, how)
        if path.startswith("/game/"):
            name = urllib.parse.unquote(
                re.sub(r"^/game/|\.m3u8?$|\.xml$", "", path))
            metas = game_streams(name, _int(qs, "n", 100))
            for m in metas:
                m["group"] = name
            return sort_metas(metas, how)
        if path.startswith("/games") or path.startswith("/all"):
            return discover_cached(
                games=_int(qs, "games", 100), per_game=_int(qs, "per", 100),
                top=_int(qs, "top", 30),
                language=(qs.get("lang") or [""])[0],
                deep=_flag(qs, "deep"), workers=_int(qs, "workers", 8),
                sort=how)
        metas = []
        if _flag(qs, "mine", default=True):
            chans = read_channels(self.channels_path)
            info = channel_info(chans)
            want_all = _flag(qs, "all", default=False)
            metas = [dict(info[c], group="Twitch") for c in chans
                     if want_all or info[c]["live"]]
        # channels.txt keeps its hand-written order; discovery gets sorted
        return self._dedupe(metas + self._discovered(qs))

    def _epg_url(self, path: str, qs: dict) -> str:
        src = "playlist"
        if path.startswith("/top"):
            src = "top"
        elif path.startswith("/games") or path.startswith("/all"):
            src = "games"
        elif path.startswith("/game/"):
            src = "game/" + urllib.parse.unquote(
                re.sub(r"^/game/|\.m3u8?$", "", path))
        params = {k: v[0] for k, v in qs.items() if k != "q"}
        params["src"] = src                      # quoted exactly once
        if self.access_key:
            params["key"] = self.access_key
        return f"{self.base_url()}/epg.xml?" + urllib.parse.urlencode(params)

    def _discovered(self, qs: dict) -> list[dict]:
        """Optional ?top= / ?games= / ?per= discovery mixed into a playlist."""
        games, top = _int(qs, "games", 0), _int(qs, "top", 0)
        if not games and not top:
            return []
        return discover_cached(
            games=games, per_game=_int(qs, "per", 100), top=top,
            language=(qs.get("lang") or [""])[0], deep=_flag(qs, "deep"),
            workers=_int(qs, "workers", 8),
            sort=(qs.get("sort") or ["viewers"])[0])

    @staticmethod
    def _dedupe(metas: list[dict]) -> list[dict]:
        seen, out = set(), []
        for m in metas:
            if m["login"] not in seen:
                seen.add(m["login"])
                out.append(m)
        return out

    def base_url(self) -> str:
        """Public origin of this request.

        Behind a TLS reverse proxy the scheme is https and Host carries no
        port; emitting http://host:7777 there produces a playlist full of
        unreachable URLs.
        """
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0]
        host = (self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host") or "")
        host = host.split(",")[0].strip()
        if not host:
            host = f"127.0.0.1:{self.server.server_address[1]}"
        elif ":" not in host and not proto:
            host = f"{host}:{self.server.server_address[1]}"
        return f"{proto or 'http'}://{host}"

    def _m3u(self, metas: list[dict], quality: str, epg_url: str = ""):
        body = playlist_from_meta(
            metas, direct=False, base=self.base_url(), quality=quality,
            proxy=self.proxy_default, epg_url=epg_url, key=self.access_key)
        return self._text(body, "application/vnd.apple.mpegurl")

    def _text(self, body: str, ctype: str = "text/plain; charset=utf-8"):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _help(self) -> str:
        h, p = self.server.server_address[0], self.server.server_address[1]
        return (f"twitch_m3u redirect server  (this page: /help)\n\n"
                f"  http://{h}:{p}/                     same as below — a "
                f"player can use the bare host\n"
                f"  http://{h}:{p}/playlist.m3u8        live channels from "
                f"channels.txt\n"
                f"  http://{h}:{p}/playlist.m3u8?all=1  include offline ones\n"
                f"  http://{h}:{p}/live/<channel>.m3u8  one channel\n"
                f"  http://{h}:{p}/live/<channel>.m3u8?q=720p60\n"
                f"  http://{h}:{p}/vod/<video-id>.m3u8  a past broadcast\n"
                f"\nlots of channels:\n"
                f"  http://{h}:{p}/games.m3u8           top 30 + top 20 in "
                f"each of the top 10 categories\n"
                f"  http://{h}:{p}/games.m3u8?games=20&per=30\n"
                f"  http://{h}:{p}/top.m3u8             directory front page "
                f"(30 max)\n"
                f"  http://{h}:{p}/game/VALORANT.m3u8   one category\n"
                f"  http://{h}:{p}/playlist.m3u8?games=10   yours + "
                f"discovery\n\n"
                f"q = best | worst | audio | master | 1080p60 | 720p | 480p\n")


def serve(host: str, port: int, quality: str, channels_path: str,
          refresh: float = 900.0, key: str = "") -> None:
    global DISCOVER_TTL
    Handler.default_quality = quality
    Handler.channels_path = channels_path
    Handler.access_key = key or os.environ.get("TWITCH_M3U_KEY", "").strip()
    if refresh:
        DISCOVER_TTL = max(DISCOVER_TTL, refresh * 3)   # refresher keeps it hot
    ThreadingHTTPServer.allow_reuse_address = True   # survive quick restarts
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno in (48, 98):                      # EADDRINUSE
            raise SystemExit(
                f"port {port} is already in use — another copy is probably "
                f"still running.\n"
                f"  stop it:      pkill -f 'twitch_m3u.py serve'\n"
                f"  or pick one:  twitch_m3u.py serve --port {port + 1}")
        raise
    httpd.daemon_threads = True
    stop = threading.Event()
    if refresh:
        threading.Thread(target=refresh_loop, args=(refresh, stop),
                         daemon=True).start()
    print(f"twitch_m3u serving on http://{host}:{port}\n"
          f"  playlist: http://{host}:{port}/games.m3u8\n"
          f"  guide:    http://{host}:{port}/epg.xml?src=games\n"
          f"  channels: {channels_path}\n"
          + (f"  refresh:  every {refresh / 60:.0f} min\n" if refresh
             else "  refresh:  off\n")
          + ("  access:   ?key= required\n" if Handler.access_key
             else "  access:   OPEN — anyone who can reach this can use it\n")
          + "Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop.set()
        httpd.server_close()


# ------------------------------------------------------------------------- cli

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="twitch_m3u", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve", help="print a playable HLS URL")
    p.add_argument("channel", help="channel login, twitch.tv URL, or VOD id")
    p.add_argument("-q", "--quality", default="best")
    p.add_argument("--list", action="store_true",
                   help="list every available quality instead")

    p = sub.add_parser("serve", help="run the redirect server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("-q", "--quality", default="best")
    p.add_argument("-c", "--channels", default=CHANNELS_FILE)
    p.add_argument("--key", default="", metavar="SECRET",
                   help="require ?key=SECRET on every request "
                        "(or set TWITCH_M3U_KEY)")
    p.add_argument("--refresh", type=float, default=900, metavar="SECONDS",
                   help="rescan in the background this often (0 = off, "
                        "default 900 = 15 min)")

    p = sub.add_parser("build", help="write an .m3u file")
    p.add_argument("-o", "--out", default=os.path.join(HERE, "twitch.m3u"))
    p.add_argument("-c", "--channels", default=CHANNELS_FILE)
    p.add_argument("--direct", action="store_true",
                   help="bake current usher URLs in (expires; no server)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("-q", "--quality", default="best")
    p.add_argument("--all", action="store_true",
                   help="include offline channels (server mode only)")
    p.add_argument("--top", type=int, default=0, metavar="N",
                   help="also add the top N live channels (max 30)")
    p.add_argument("--games", type=int, default=0, metavar="N",
                   help="also add the top N categories, grouped")
    p.add_argument("--per", type=int, default=100, metavar="N",
                   help="channels per category (max 100)")
    p.add_argument("--lang", default="", metavar="EN",
                   help="restrict discovery to a language")
    p.add_argument("--no-mine", action="store_true",
                   help="skip channels.txt, discovery only")
    p.add_argument("--redirect", action="store_true",
                   help="use plain /live redirects instead of the /hls proxy")
    p.add_argument("--deep", action="store_true",
                   help="widen past 100 categories by searching the alphabet")
    p.add_argument("--workers", type=int, default=8, metavar="N")
    p.add_argument("--sort", default="viewers",
                   choices=["viewers", "asc", "name", "none"],
                   help="channel order (default viewers, highest first)")

    p = sub.add_parser("discover", help="browse what is live right now")
    p.add_argument("--top", type=int, default=30, metavar="N")
    p.add_argument("--games", type=int, default=0, metavar="N")
    p.add_argument("--per", type=int, default=20, metavar="N")
    p.add_argument("--lang", default="", metavar="EN")
    p.add_argument("--game", default="", help="one category by name")
    p.add_argument("--deep", action="store_true")
    p.add_argument("--workers", type=int, default=8, metavar="N")
    p.add_argument("--append", action="store_true",
                   help="append the logins to channels.txt")
    p.add_argument("-c", "--channels", default=CHANNELS_FILE)

    p = sub.add_parser("status", help="show who is live")
    p.add_argument("-c", "--channels", default=CHANNELS_FILE)

    a = ap.parse_args(argv)

    if a.cmd == "resolve":
        target = re.sub(r"^https?://(www\.)?twitch\.tv/", "",
                        a.channel).strip("/")
        vod = target if target.isdigit() else ""
        ch = "" if vod else target.lower()
        try:
            if a.list:
                _, body = master_playlist(ch, vod)
                for v in sorted(variants(body),
                                key=lambda v: -v["bandwidth"]):
                    print(f'{v["group"]:<12} {v["name"]:<18} '
                          f'{v["resolution"] or "-":<10} '
                          f'{v["bandwidth"]//1000:>6} kbps')
                return 0
            print(resolve(ch, a.quality, vod_id=vod))
        except Offline as e:
            print(f"offline: {e}", file=sys.stderr)
            return 1
        except TwitchError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0

    if a.cmd == "serve":
        serve(a.host, a.port, a.quality, a.channels, a.refresh, a.key)
        return 0

    if a.cmd == "build":
        metas = []
        if not a.no_mine:
            chans = read_channels(a.channels)
            info = channel_info(chans)
            keep = a.all and not a.direct
            metas = [dict(info[c], group="Twitch") for c in chans
                     if keep or info[c]["live"]]
        if a.top or a.games:
            metas += discover(games=a.games, per_game=a.per, top=a.top,
                              language=a.lang, deep=a.deep,
                              workers=a.workers, progress=True, sort=a.sort)
        seen, uniq = set(), []
        for m in metas:
            if m["login"] not in seen:
                seen.add(m["login"])
                uniq.append(m)
        body = playlist_from_meta(uniq, direct=a.direct, host=a.host,
                                  port=a.port, quality=a.quality,
                                  proxy=not a.redirect)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(body)
        n = sum(1 for line in body.splitlines() if line.startswith("#EXTINF"))
        groups = len({m.get("group") for m in uniq})
        print(f"wrote {a.out} — {n} channels in {groups} group(s)"
              + ("  (baked URLs: re-run when they expire)" if a.direct
                 else "  (needs: twitch_m3u.py serve)"))
        return 0

    if a.cmd == "discover":
        if a.game:
            metas = sort_metas(game_streams(a.game, a.per or 30))
            for m in metas:
                m["group"] = a.game
        else:
            metas = discover(games=a.games, per_game=a.per, top=a.top,
                             language=a.lang, deep=a.deep,
                             workers=a.workers, progress=True)
        for m in metas:
            print(f'● {m["login"]:<22} {m["viewers"]:>7,}  '
                  f'{(m.get("group") or "")[:24]:<26}{m["title"][:44]}')
        if a.append:
            have = set(read_channels(a.channels)) if os.path.exists(
                a.channels) else set()
            fresh = [m["login"] for m in metas if m["login"] not in have]
            with open(a.channels, "a", encoding="utf-8") as f:
                if fresh:
                    f.write("\n# added by discover\n")
                    f.write("\n".join(fresh) + "\n")
            print(f"\nappended {len(fresh)} new channel(s) to {a.channels}")
        else:
            print(f"\n{len(metas)} live channels "
                  f"(add --append to save them to channels.txt)")
        return 0

    if a.cmd == "status":
        chans = read_channels(a.channels)
        info = channel_info(chans)
        for ch in chans:
            m = info[ch]
            if m["live"]:
                print(f'● {m["display"]:<20} {m["viewers"]:>7,}  '
                      f'{m["game"][:22]:<24}{m["title"][:50]}')
            else:
                print(f'○ {m["display"]:<20} {"offline":>7}')
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
