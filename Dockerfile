# ---------------------------------------------------------------------------
# DVR caseiro: FastAPI + FFmpeg + cloudflared em um único container.
# Base Debian slim com Python 3.12 (mesma família do host Ubuntu 24.04).
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1

# ffmpeg    -> captura v4l2 + HLS/segmentação
# v4l-utils -> v4l2-ctl (brilho, exposição, filtros de IR da câmera)
# tzdata    -> respeitar TZ (America/Sao_Paulo) nos nomes de arquivo
# curl      -> download do cloudflared
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        v4l-utils \
        tzdata \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# cloudflared fica DENTRO do container (não depende do host).
ARG CLOUDFLARED_VERSION=latest
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) cf_arch="amd64" ;; \
        arm64) cf_arch="arm64" ;; \
        *) echo "arquitetura não suportada: $arch" >&2; exit 1 ;; \
    esac; \
    if [ "$CLOUDFLARED_VERSION" = "latest" ]; then \
        url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}"; \
    else \
        url="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${cf_arch}"; \
    fi; \
    curl -fsSL "$url" -o /usr/local/bin/cloudflared; \
    chmod +x /usr/local/bin/cloudflared; \
    cloudflared --version

WORKDIR /app

# Dependências primeiro (melhor cache de camadas).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação e frontend.
COPY app ./app
COPY static ./static

# Diretórios de dados (normalmente sobrescritos por volumes no compose).
RUN mkdir -p /app/recordings /app/hls

EXPOSE 2310

# Worker ÚNICO: o estado (streamer, gravador, túnel) vive no processo.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "2310", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
