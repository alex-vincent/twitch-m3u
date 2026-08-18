# twitch_m3u

An M3U playlist of Twitch livestreams that any HLS player (VLC, mpv, IINA,
ffmpeg, Jellyfin, Tivimate, Kodi…) can open.

## Why it isn't just a text file

Twitch has no stable `.m3u8` per channel. A playable URL has to be minted:
ask `gql.twitch.tv` for a signed `PlaybackAccessToken`, hand that to
`usher.ttvnw.net`, and get back an HLS master playlist. Those URLs expire, and
an offline channel has none at all — so a playlist with URLs baked into it goes
stale within the hour.

Hence two modes:

| mode | playlist entries point at | expires? | needs a process running? |
|---|---|---|---|
| **serve** (default) | `http://127.0.0.1:7777/live/<channel>.m3u8` | never | yes, the redirect server |
| **`build --direct`** | the current usher URL | yes, quickly | no |

In serve mode the little local server does the token dance on every open and
answers with a `302` to a fresh URL, so the `.m3u` file itself stays valid
forever and offline channels start working the moment they go live.

## Use it

Put your channels in `channels.txt` (one login or twitch.tv URL per line), then:

```bash
python3 twitch_m3u.py serve
```

Open `http://127.0.0.1:7777/playlist.m3u8` directly in your player — it is
generated live, so it always reflects who is streaming right now. Or write a
file to hand to an app that wants one:

```bash
python3 twitch_m3u.py build -o twitch.m3u
```

Other commands:

```bash
python3 twitch_m3u.py status                    # who's live, viewers, title
python3 twitch_m3u.py resolve caedrel            # print one playable URL
python3 twitch_m3u.py resolve caedrel --list     # what qualities exist
python3 twitch_m3u.py resolve 2849020183         # a VOD, by video id
python3 twitch_m3u.py build --direct             # no-server playlist
```

Play something without a playlist at all:

```bash
mpv "$(python3 twitch_m3u.py resolve caedrel -q 720p60)"
```

## Getting lots of channels

`channels.txt` is just your own shortlist, and the playlist hides whoever is
offline — so a list of six names shows one channel on a quiet afternoon. To
fill it out, pull from Twitch's directory instead:

```
/games.m3u8              ~9,000 channels across the top 100 categories
/games.m3u8?deep=1       ~33,000 channels across ~3,400 categories
/games.m3u8?games=30&per=50    a smaller cut, if your app struggles
/top.m3u8                the directory front page (30, the hard cap)
/game/VALORANT.m3u8      one category  (/game/Just%20Chatting.m3u8)
/playlist.m3u8?games=10  your channels.txt plus discovery
/playlist.m3u8?all=1     your channels.txt including the offline ones
```

Each category becomes its own `group-title`, so the app shows them as folders.
Add `?lang=EN` to any of them to restrict by language.

Point your app at `/games.m3u8` and refresh it whenever you want a fresh cut —
it's generated on the spot, so it always reflects who is live at that moment.

Same thing from the CLI:

```bash
python3 twitch_m3u.py build --games 100 --per 100 -o twitch.m3u   # ~9k channels
python3 twitch_m3u.py build --games 100 --per 100 --deep          # ~33k
python3 twitch_m3u.py discover --game VALORANT --append   # save to channels.txt
python3 twitch_m3u.py build --no-mine --top 30            # discovery only
```

### How big it goes

Measured yields (categories are fetched in parallel, 8 workers by default):

| command | channels | groups | time |
|---|---|---|---|
| `--games 10 --per 25` | ~200 | 11 | <1s |
| `--games 100 --per 100` | **8,856** | 100 | 3s |
| `--games 100 --per 100 --deep` | **32,778** | 1,584 | 43s |

`--deep` stops relying on the top-100 category list and searches the alphabet
for category names instead, which surfaced 3,454 categories.

Note that 30k entries will make some IPTV apps crawl or hang on import. If
yours struggles, `--games 100 --per 100` (~9k) is the comfortable sweet spot,
or narrow it with `--games 30`.

### The real per-request caps

These differ per field, and assuming a single limit is what keeps a playlist
small:

| query | cap |
|---|---|
| `games` (category list) | 100 |
| `game(name).streams` | 100 |
| `searchCategories` | 100 per term |
| global `streams` (the front page) | 30 |

Only the global front page is truly limited to 30. Paging past any of these
with a cursor is gated behind an integrity challenge, which this tool does not
attempt to defeat — breadth comes from fanning out across categories instead,
each of which is its own un-gated request.

## "Commercial break in progress" forever

The stream is not stuck — the player is. Measured on a live preroll:

```
 t    MEDIA-SEQUENCE   ad?   segments
 0                 0  True          4
30                 0  True         18
54                 0  True         30
60                18 False         15   <- content resumed on the same URL
```

During an ad the media playlist behaves like a VOD: `MEDIA-SEQUENCE` pins to 0
and the window grows, then snaps forward when content returns. Players that
assume a monotonic sequence call that fatal and never recover.

Worse, the ad is a **preroll** — Twitch serves one when a *new playback session*
starts. So a player that stalls and retries earns a fresh 30s ad every attempt.
That is the "forever" part.

Both are fixed:

- **`/hls/<channel>.m3u8`** proxies the manifest and renumbers it so the
  sequence only moves forward. The ad still plays; the player survives it and
  returns to content. Playlists point here by default.
- **Sessions are now stable per channel**, so a reconnect continues the old
  session instead of triggering another preroll, and resolved URLs are cached
  for 10 minutes rather than 20 seconds.

`/live/<channel>.m3u8` still does the plain redirect if you prefer it.

### Actually removing the ads

Twitch decides that server-side, from your account. The playback token comes
back with `hide_ads`, `show_ads`, `turbo` and `subscriber` flags on it — with
Turbo, or a sub to that channel, Twitch issues an ad-free token and the
discontinuity that causes the stall never appears at all.

If you have one of those, export your own OAuth token and the tool will use it:

```bash
export TWITCH_AUTH_TOKEN=...      # your account; read from the env, never logged
```

Beyond that, this tool does not try to defeat ad delivery — no region-shifting
proxies, no stripping ad segments out of the manifest.

## Stream titles and viewer counts (the EPG)

IPTV apps show "No information available" unless an XMLTV guide is attached, so
the server generates one. Every playlist advertises its own guide via
`x-tvg-url`, which most apps pick up automatically on import.

Each live stream becomes one programme:

```xml
<programme start="20260817170226 +0000" stop="20260818121037 +0000" channel="palumor">
  <title>NEX -&gt; TOA WITH THE BOYS + @SARDACO TODAY | !GOALS !24HR !TILES</title>
  <desc>Old School RuneScape · 171 viewers</desc>
  <category>Old School RuneScape</category>
</programme>
```

`start` is the stream's real `createdAt`, so the guide bar begins when the
broadcast actually began rather than at an arbitrary hour.

The channel row itself now carries the viewer count too:

```
Palumor · 171 viewers · Old School RuneScape
```

If your app wants the guide URL entered by hand:

```
http://127.0.0.1:7777/epg.xml?src=games          matches /games.m3u8
http://127.0.0.1:7777/epg.xml?src=playlist       matches /playlist.m3u8
http://127.0.0.1:7777/epg.xml?src=top            matches /top.m3u8
```

Pass the same `games=` / `per=` / `deep=` values you used for the playlist so
the two line up; discovery is cached for two minutes, so the guide is served
from the same channel set the playlist was built from rather than a fresh scan.
Guide entries are counted, not estimated: a 987-channel playlist produced 987
programmes, all with titles and viewer counts.

Titles and counts are a snapshot from when the guide was fetched. Re-fetch it
in your app to refresh them.

## Keeping it current

Two halves, and only one is mine to fix.

**Server side** — already handled. `serve` rescans in the background every 15
minutes by default and overwrites its cache, so a fetch is both current and
instant even when the scan itself takes a while:

```bash
python3 twitch_m3u.py serve --refresh 900     # default, 15 min
python3 twitch_m3u.py serve --refresh 300     # every 5 min
python3 twitch_m3u.py serve --refresh 0       # off, rebuild on demand
```

It only refreshes param-sets a client has actually requested, so it stays idle
until something asks for a playlist and never scans combinations nobody wants.
Measured over one 60s cycle on a 550-channel set: 139 viewer counts changed, 17
channels went offline, 20 came online, the rescan took 0.7s, and the subsequent
client fetch was served warm in ~0.00s. The EPG follows the same snapshot.

**App side** — your call. None of the above matters if your player never
re-fetches. Playlists and the guide are sent with `Cache-Control: no-cache`, but
most IPTV apps only reload on their own schedule; look for a playlist
auto-update or EPG refresh interval in its settings and set it to match. A
shorter `--refresh` than your app's own interval just burns API calls.

A deep scan is ~3,400 requests. At `--refresh 300` with `deep=1` that is a lot
of traffic every five minutes — pair deep scans with a longer interval.

## Quality

`-q` on any command, or `?q=` on any server URL:

`best` (default) · `worst` · `audio` · `1080p60` · `720p60` · `720` · `480p` ·
`master`

A bare number picks the nearest at-or-below that height. `master` returns the
multi-variant playlist and lets the player do its own adaptive switching —
good for VLC/mpv on a flaky connection, bad for players that mishandle ABR.

## Server endpoints

```
/                       same as /playlist.m3u8 (a player can use the bare host)
/playlist.m3u8          live channels from channels.txt
/playlist.m3u8?all=1    include offline ones (they 404 until they go live)
/playlist.m3u8?games=10 channels.txt + discovery
/top.m3u8               top 30 live channels
/games.m3u8             top live across the top categories, grouped
/game/<name>.m3u8       one category
/hls/<channel>.m3u8     one channel, ad-stall-proof (default)  (+ ?q=720p60)
/live/<channel>.m3u8    one channel, plain 302 redirect
/vod/<video-id>.m3u8    a past broadcast
/epg.xml?src=games      XMLTV guide: titles, viewer counts, categories
/help                   this list
```

It binds to `127.0.0.1` only. To reach it from a TV or phone on your LAN,
`--host 0.0.0.0` and build the playlist with `--host <your-lan-ip>`. That
exposes it to everything on the network — no auth — so only on a network you
trust.

## Notes

- Anonymous playback only: sub-only and other gated streams won't resolve, and
  you'll see ads the same as on the site.
- Resolutions other than source depend on the channel being transcoded; small
  channels often only offer `chunked` (source). `--list` shows what's there.
- Stdlib Python 3.9+ only. No dependencies, no API key, no login.
