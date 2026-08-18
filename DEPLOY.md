# Hosting twitch_m3u on an Oracle Cloud VM

Target used throughout: `twitch.vincentserver.com` → `192.18.147.171`.

Run everything after step 2 on the VM, over SSH.

---

## 0. Read this first

Before spending an hour on setup, know the one thing that can sink it: the
playback token embeds the IP that requested it. If Twitch enforces that
binding, tokens minted on the VM may be rejected when a viewer's player fetches
segments from a different IP — and the whole design fails for anyone not on the
VM itself. **Step 8 tests exactly this.** Consider doing a minimal install and
running step 8 before bothering with TLS.

Also: this instance will be publicly reachable. Set an access key (step 6) or
anyone who finds the hostname can burn your Twitch API quota, and every request
they make comes from your IP.

## 1. DNS

At whoever hosts `vincentserver.com`, add:

| type | name | value | TTL |
|---|---|---|---|
| A | `twitch` | `192.18.147.171` | 300 |

Verify from your Mac (should print the IP):

```bash
dig +short twitch.vincentserver.com
```

## 2. Open the port in OCI

Oracle blocks inbound traffic in **two** independent places. Both must be done.

Console → Networking → Virtual Cloud Networks → your VCN → Security Lists →
default list → **Add Ingress Rule**:

- Source CIDR: `0.0.0.0/0`
- IP Protocol: TCP
- Destination Port Range: `80,443`

## 3. Open the port on the host

Oracle images ship with an iptables rule that drops everything except SSH.
This is the step people miss — the console says open, the port still refuses.

Oracle Linux / RHEL:

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https && sudo firewall-cmd --reload
```

Ubuntu images (iptables directly):

```bash
sudo iptables -I INPUT 5 -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT && sudo iptables -I INPUT 6 -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT && sudo netfilter-persistent save
```

## 4. Install

```bash
sudo dnf install -y git python3 || sudo apt update && sudo apt install -y git python3
```

```bash
sudo git clone https://github.com/alex-vincent/twitch-m3u.git /opt/twitch-m3u
```

Nothing to pip install — it is stdlib only.

## 5. Generate an access key

```bash
openssl rand -hex 16
```

Keep the output; it goes in the service file and in every URL you hand out.

## 6. Run it as a service

```bash
sudo tee /etc/systemd/system/twitch-m3u.service >/dev/null <<'UNIT'
[Unit]
Description=twitch_m3u
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/twitch-m3u/twitch_m3u.py serve --host 127.0.0.1 --port 7777 --refresh 900
Environment=TWITCH_M3U_KEY=PASTE_YOUR_KEY_HERE
DynamicUser=yes
Restart=always
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
UNIT
```

Paste your key into that file, then:

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now twitch-m3u && sudo systemctl status twitch-m3u --no-pager
```

It binds to `127.0.0.1` only — Caddy is what faces the internet.

## 7. TLS + reverse proxy

```bash
sudo dnf install -y caddy || sudo apt install -y caddy
```

```bash
sudo tee /etc/caddy/Caddyfile >/dev/null <<'CADDY'
twitch.vincentserver.com {
    reverse_proxy 127.0.0.1:7777
}
CADDY
```

```bash
sudo systemctl restart caddy
```

Caddy gets a Let's Encrypt certificate automatically and sets the
`X-Forwarded-Proto` / `Host` headers the server needs to emit correct URLs.

## 8. The test that decides everything

**From a device on a different network — phone on cellular, not your wifi:**

```bash
curl -s "https://twitch.vincentserver.com/top.m3u8?n=1&key=YOURKEY" | tail -1
```

Take the `https://twitch.vincentserver.com/hls/...` URL it prints and open it in
VLC on that device.

- **It plays** → token IP binding is not enforced. You are done.
- **403 / forbidden / stalls immediately** → tokens are bound to the minting IP.
  The redirect and manifest-proxy designs both fail for remote viewers, and the
  only fix is proxying the video segments themselves through the VM, which
  means ~4 GB/hr per viewer on a free tier with a 10 TB/month cap. At that point
  a VPN back to your home network is the better answer.

## 9. Point your app at it

```
https://twitch.vincentserver.com/games.m3u8?key=YOURKEY
```

The guide attaches automatically; the key is carried into every generated URL.

## Operating it

```bash
sudo journalctl -u twitch-m3u -f
```

```bash
cd /opt/twitch-m3u && sudo git pull && sudo systemctl restart twitch-m3u
```

## If you would rather not expose it

If the goal is only reaching your own TV and phone, skip steps 1–3 and 7 and
put both devices on a Tailscale network with the VM. You get the same result
with no public surface, no TLS to manage, and no open instance to find.
