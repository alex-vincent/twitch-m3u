FROM python:3.12-slim
WORKDIR /app
COPY twitch_m3u.py web.html channels.txt ./
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 7777
# Tokens and playlists go through the VPN; segments go player -> CDN.
# Set TWITCH_M3U_FULL_PROXY=1 to carry segments through the tunnel too.
CMD ["python", "twitch_m3u.py", "serve", "--host", "0.0.0.0"]
