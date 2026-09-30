#!/bin/sh
set -e

mkdir -p "$DATA_DIR" "$HF_HOME" "$TORCH_HOME"

# YouTube casse régulièrement les anciennes versions de yt-dlp : mise à jour au démarrage
if [ "${YTDLP_AUTO_UPDATE:-true}" = "true" ]; then
    uv pip install --python /opt/venv/bin/python --quiet --upgrade "yt-dlp[default]" \
        || echo "yt-dlp : mise à jour impossible, version de l'image conservée" >&2
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 15
