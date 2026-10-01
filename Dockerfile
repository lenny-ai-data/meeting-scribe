# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11 AS uv
FROM denoland/deno:bin AS deno

FROM ubuntu:24.04

# Variante : cuda (GPU NVIDIA, amd64) ou cpu (amd64 et arm64)
ARG FLAVOR=cuda
ARG DEFAULT_DEVICE=cuda

ENV DEBIAN_FRONTEND=noninteractive \
    DEFAULT_DEVICE=${DEFAULT_DEVICE} \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/opt/venv/bin:$PATH \
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    MPLCONFIGDIR=/models/matplotlib \
    DATA_DIR=/data \
    TZ=Europe/Paris \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    # cuDNN et cuBLAS viennent des roues pip de torch : ctranslate2 (faster-whisper) doit les trouver aussi
    LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib

RUN apt-get update \
    # libpython3.12 : chargée par torchcodec (pyannote)
 && apt-get install -y --no-install-recommends python3.12 python3.12-venv libpython3.12t64 ffmpeg ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/
# Moteur JavaScript requis par yt-dlp pour YouTube
COPY --from=deno /deno /usr/local/bin/deno

# Utilisateur « ubuntu » (uid 1000) de l'image : les fichiers de ./data et ./models appartiennent à l'utilisateur de l'hôte
RUN mkdir -p /opt/venv /opt/models /app /data /models && chown ubuntu:ubuntu /opt/venv /opt/models /app /data /models
USER ubuntu
WORKDIR /app

COPY --chown=ubuntu:ubuntu pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache,uid=1000,gid=1000 \
    uv sync --frozen --no-dev --extra ${FLAVOR} --no-install-project

# Modèle de diarisation embarqué (CC-BY-4.0, redistribuable avec attribution) : les utilisateurs
# n'ont besoin ni de jeton Hugging Face ni de réseau. Le jeton n'est lu qu'à la construction
# (secret BuildKit hf_token) et ne reste pas dans l'image. Sans secret, repli sur HF_TOKEN à l'exécution.
RUN --mount=type=secret,id=hf_token,uid=1000 \
    if [ -s /run/secrets/hf_token ]; then \
      HF_TOKEN="$(cat /run/secrets/hf_token)" HF_HOME=/tmp/hf python -c "from huggingface_hub import snapshot_download; \
snapshot_download('pyannote/speaker-diarization-community-1', local_dir='/opt/models/pyannote/speaker-diarization-community-1', \
ignore_patterns=['*.gif', '.gitattributes'])" \
      && rm -rf /opt/models/pyannote/speaker-diarization-community-1/.cache /tmp/hf; \
    else echo "Pas de secret hf_token : modèle de diarisation non embarqué (HF_TOKEN requis à l'exécution)"; fi

COPY --chown=ubuntu:ubuntu app ./app
COPY --chown=ubuntu:ubuntu web ./web
COPY --chown=ubuntu:ubuntu docker/entrypoint.sh /app/entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
