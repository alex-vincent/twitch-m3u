# PIA Albania: local testing and Oracle VM

This optional Docker Compose stack sends the Python service's outbound traffic
through Private Internet Access's Albania OpenVPN region. Gluetun supplies the
VPN and firewall kill switch. Twitch M3U waits for Gluetun to be healthy at
startup and shares its network namespace, so every request the server makes
itself (playback tokens, usher, playlists) leaves through the VPN. There is no
non-VPN service in this stack and no direct-player fallback when those
requests fail.

## What goes through the tunnel

By default only the server's own requests do. Playlists served from `/hls`
keep Twitch's segment URLs, so the player downloads video straight from
Twitch's CDN over its own connection. Measured in September 2026 from a
Toronto VM with the Albania exit: the tunnel sustained about 1-5 Mbit/s with a
~1 s TLS handshake per connection, while a source-quality stream needs about
7 Mbit/s, so carrying segments through it buffered constantly. Twitch did not
enforce IP binding: playlist and segment URLs minted through the Albania exit
played from a different IP.

Set `TWITCH_M3U_FULL_PROXY=1` in `.env` (or run `serve --full-proxy`) to carry
manifests, nested quality/audio playlists, segments, encryption keys, and
initialization files through the server as well. `/live` and `/vod` are also
proxied in that mode. It keeps viewers' IPs away from Twitch's CDN at the cost
of every viewer's bandwidth crossing the tunnel (roughly 3.6 GB/hour at
8 Mbps, before overhead), which only works on a fast tunnel. If Twitch starts
rejecting segment fetches from an IP other than the token's, this is the
fallback.

`resolve` and `build --direct` still produce upstream URLs: do not use those
for VPN playback on a separate device.

**Ads are not guaranteed to disappear.** This selects a PIA region; it does not
change Twitch entitlement flags or remove ads from streams. A PIA subscription
is required.

## Prerequisites

- Docker Engine + Docker Compose v2.17+ on the Oracle VM; Docker Desktop or
  another Docker Linux runtime on macOS.
- The Linux Docker host must have `/dev/net/tun`. The VPN container receives
  NET_ADMIN; the Python container runs unprivileged.
- Outbound traffic to PIA must be allowed by the VM/network firewall.
- Run commands in the repository directory.

## Local test

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 24
```

Edit `.env`: enter your PIA username/password and use the generated value as
`TWITCH_M3U_KEY`. Set `TWITCH_M3U_PORT=7778` if your existing local server is on
7777. Single-quote credentials, particularly those containing `$` or `#`.
The file is ignored by Git and excluded from the Docker build context. Never
paste credentials into issues or commit them. Compose configuration output can
contain them, so use `config --quiet` for validation.

```bash
docker compose -f compose.pia.yml config --quiet
docker compose -f compose.pia.yml run --rm --no-deps gluetun update -enduser -providers "private internet access"
docker compose -f compose.pia.yml up -d --build
docker compose -f compose.pia.yml ps
docker compose -f compose.pia.yml logs --tail=60 gluetun
```

Verify Gluetun reports Albania and a healthy VPN. Confirm the application shares
that public exit IP:

```bash
docker compose -f compose.pia.yml exec twitch-m3u python -c 'import urllib.request; print(urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode())'
```

Open in your player (adjust port and replace YOUR_KEY):

```text
http://127.0.0.1:7778/games.m3u8?games=5&per=10&key=YOUR_KEY
```

In the default mode, `/hls/<channel>.m3u8` must be served by this server and
list segment URLs on `*.ttvnw.net`. With `TWITCH_M3U_FULL_PROXY=1`, all
playable URLs, including URLs inside a channel's manifest, must point at this
server's `/media` endpoint instead, and a channel has to be reopened after the
app container restarts because media signatures are process-local. Test
`?q=master` as well as the default quality. Thumbnails may still be loaded
directly by the IPTV app; they are not playback traffic.

To inspect the configured region list without credentials:

```bash
docker run --rm qmcgaw/gluetun:v3.41.3 format-servers -private-internet-access
```

The server list is stored in a named volume and refreshed every 480 hours.
Run the update command above before first startup; bundled endpoints can be stale.

If Albania is unavailable, the stack must fail rather than silently select a
different country. Check Gluetun's logs and provider/server updates.

## Oracle VM migration

The existing Oracle installation runs this stack from `/opt/twitch-m3u-pia`,
with its private configuration in `/opt/twitch-m3u-pia/.env`. Use that directory
for updates and Compose commands. `/opt/twitch-m3u` remains the original
systemd installation for rollback. The instructions below also cover a fresh
migration.

Deploy this version of the repository to `/opt/twitch-m3u`. Keep a copy of your
existing `channels.txt` before replacing a checkout. Configure `.env` on the VM
as above, preserving the **existing** `TWITCH_M3U_KEY` so playlist URLs continue
to work. Initially set `TWITCH_M3U_PORT=7778` and test the new stack while the old
service remains on 7777. You can test from your Mac with an SSH tunnel:

```bash
ssh -L 7778:127.0.0.1:7778 USER@VM
```

After the VPN and playback checks pass, stop the old service:

```bash
sudo systemctl stop twitch-m3u
```

Set `TWITCH_M3U_PORT=7777` in the VM's `.env`.
On the current Oracle deployment Caddy is containerized and uses
`reverse_proxy 172.19.0.1:7777`. Set `TWITCH_M3U_BIND_IP=172.19.0.1` on Oracle
before cutover to preserve that upstream. For local tests and Caddy running
directly on the host, leave `TWITCH_M3U_BIND_IP=127.0.0.1`.

Recreate **both** containers so they share the current VPN network namespace:

```bash
docker compose -f compose.pia.yml up -d --force-recreate
docker compose -f compose.pia.yml ps
```

For host-installed Caddy the configuration is:

```caddyfile
twitch.vincentserver.com {
    reverse_proxy 127.0.0.1:7777
}
```

No new public port is needed. Compose publishes to the configured host IP
(loopback by default, the Docker bridge for the current Oracle Caddy). Test your
usual HTTPS playlist from a phone on cellular, and verify playback and the EPG.
Once successful, disable the old service so it does not compete for the port
after a reboot:

```bash
sudo systemctl disable twitch-m3u
```

Rollback if needed:

```bash
docker compose -f compose.pia.yml down
sudo systemctl enable --now twitch-m3u
```

Rollback restores the original non-VPN service.

## VPN failure test

Perform a controlled VPN outage on the test stack before production cutover.
Interrupt the VPN tunnel using the Docker host's network controls while leaving
the Gluetun firewall enabled. The exit-IP command above must fail during the
outage, playlist reloads must return 503, and playback must stall once the
segments already listed have played out. It must not switch to the VM's public
IP. In the default mode the player fetches segments itself, so those keep
loading for the few seconds they remain listed; in full-proxy mode they stop
with the tunnel. Gluetun may reconnect automatically; a successful
request after reconnection is expected. Simply stopping the Gluetun container
is not conclusive, since a shared namespace can retain its tunnel.

Recover both containers after testing:

```bash
docker compose -f compose.pia.yml up -d --force-recreate
```

## Checks and limits

Upstream connections are kept alive and reused per host, so a playlist reload
or segment fetch does not pay a TLS handshake through the tunnel each time
(about a second per connection on the Albania exit). Gluetun's DNS forwarder
is set to plain DNS (`DNS_UPSTREAM_RESOLVER_TYPE: plain`): queries still
travel inside the tunnel to the same upstream resolver, but DNS-over-TLS cost a
TLS handshake per uncached name, 0.6-3.4 s measured, which dominated channel
start time.

The parser accepts relative and protocol-relative rendition URLs, CRLF line
endings, and quoted attributes containing commas. Relative links use the final
upstream URL after redirects. All Twitch token and media requests use the OS
network route; inherited `HTTP_PROXY`/`HTTPS_PROXY` settings are ignored so they
cannot put token requests and video on different exit IPs.

For `/hls`, `/live`, and `/vod`, a stale upstream response (401/403/404/410)
triggers one fresh resolution. Sequence tracking for unencrypted live streams
survives that URL refresh. VPN transport failures return a retryable response
without creating a fresh playback session or redirecting the player outside
the proxy. If an already-issued nested `/media` link expires, reopen the
channel to obtain a current manifest; arbitrary segment URLs are never
replaced with guessed equivalents.

Expected manifests are validated before they reach the player. HTML/JSON
upstream error documents are rejected instead of being served as video.
Byte-range responses and 416 errors retain their range headers; encrypted,
VOD, and byte-range playlists retain their original media sequence.

```bash
python3 -m unittest discover -s tests -v
```

Automated tests use mocked Twitch responses and real loopback HTTP requests.
They cover nested HLS rewriting, authentication, signed URLs, redirect target
validation, byte ranges, VOD sequence preservation, and upstream failures.
They do not establish VPN connectivity or prove ad-free playback. The VPN and
failure tests require Docker, PIA credentials, and a live deployment.

Media destinations are restricted to HTTPS Twitch CDN domains and each link is
signed; this is not a general-purpose URL proxy. An unrecognized CDN fails
closed and may require an allowlist update. Encrypted and completed playlists
retain their original media sequence to preserve encryption IVs.

References: [Gluetun PIA setup](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/private-internet-access.md),
[shared container networking](https://github.com/qdm12/gluetun-wiki/blob/main/setup/connect-a-container-to-gluetun.md),
[firewall options](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/firewall.md).
