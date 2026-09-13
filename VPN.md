# PIA Albania: local testing and Oracle VM

This optional Docker Compose stack sends the Python service's outbound traffic
through Private Internet Access's Albania OpenVPN region. Gluetun supplies the
VPN and firewall kill switch. Twitch M3U waits for Gluetun to be healthy at
startup and shares its network namespace. There is no non-VPN service in this
stack and no direct-player fallback when upstream requests fail.

`--full-proxy` carries manifests, nested quality/audio playlists, segments,
encryption keys, and initialization files through the server. `/live` and `/vod`
are also proxied in this mode. `resolve` and `build --direct` still produce
upstream URLs: do not use those for VPN playback on a separate device.

**Ads are not guaranteed to disappear.** This selects a PIA region; it does not
change Twitch entitlement flags or remove ads from streams. A PIA subscription
is required. Full proxying consumes VM bandwidth for every viewer (roughly
3.6 GB/hour at 8 Mbps, before overhead).

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

All playable URLs, including URLs inside a channel's manifest, must point at
this server's `/media` endpoint. Test `?q=master` as well as the default quality.
Thumbnails may still be loaded directly by the IPTV app; they are not playback
traffic. Reopen a channel after restarting the app container: media signatures
are process-local and old links then expire.

To inspect the configured region list without credentials:

```bash
docker run --rm qmcgaw/gluetun:v3.41.0 format-servers -private-internet-access
```

The server list is stored in a named volume and refreshed every 480 hours.
Run the update command above before first startup; bundled endpoints can be stale.

If Albania is unavailable, the stack must fail rather than silently select a
different country. Check Gluetun's logs and provider/server updates.

## Oracle VM migration

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

Set `TWITCH_M3U_PORT=7777` in the VM's `.env`. Recreate **both** containers so
they share the current VPN network namespace:

```bash
docker compose -f compose.pia.yml up -d --force-recreate
docker compose -f compose.pia.yml ps
```

On the current Oracle deployment Caddy is containerized and uses
`reverse_proxy 172.19.0.1:7777`. Set `TWITCH_M3U_BIND_IP=172.19.0.1` on Oracle
before cutover to preserve that upstream. For local tests and Caddy running
directly on the host, leave `TWITCH_M3U_BIND_IP=127.0.0.1`.

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
outage, and playback must stop after buffered media is consumed. It must not
switch to the VM's public IP. Gluetun may reconnect automatically; a successful
request after reconnection is expected. Simply stopping the Gluetun container
is not conclusive, since a shared namespace can retain its tunnel.

Recover both containers after testing:

```bash
docker compose -f compose.pia.yml up -d --force-recreate
```

## Checks and limits

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
