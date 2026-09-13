FROM python:3.12-slim
WORKDIR /app
COPY twitch_m3u.py channels.txt ./
USER 65532:65532
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 7777
CMD ["python", "twitch_m3u.py", "serve", "--host", "0.0.0.0", "--full-proxy"]
