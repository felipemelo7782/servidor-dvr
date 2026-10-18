"""Configuração central.

Toda a parametrização vem de variáveis de ambiente e é lida SOMENTE aqui, para
manter um único ponto de verdade sobre caminhos e limites. O restante do código
importa este módulo em vez de chamar ``os.getenv`` diretamente.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Autenticação (HTTP Basic) ---------------------------------------------
# Protege a interface e a API. O prefixo /hls/ fica de fora de propósito
# (ver auth_middleware em main.py), pois o player HLS não envia credenciais.
DVR_USER = os.getenv("DVR_USER", "admin")
DVR_PASSWORD = os.getenv("DVR_PASSWORD", "changeme")

# --- Caminhos dentro do container ------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent          # raiz do projeto
APP_DIR = BASE_DIR / "app"
STATIC_DIR = BASE_DIR / "static"
HLS_DIR = Path(os.getenv("HLS_DIR", "/app/hls"))
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))

# --- Câmera / FFmpeg -------------------------------------------------------
VIDEO_DEVICE = os.getenv("VIDEO_DEVICE", "/dev/video0")
# Formato de captura v4l2 (ex.: "mjpeg" ou "yuyv422"). Vazio = deixa o FFmpeg
# negociar. Webcams USB costumam expor MJPEG; se a imagem sair preta/ruim,
# force DVR_INPUT_FORMAT=mjpeg.
INPUT_FORMAT = os.getenv("DVR_INPUT_FORMAT", "").strip()
VIDEO_SIZE = os.getenv("DVR_VIDEO_SIZE", "640x480")
FRAMERATE = int(os.getenv("DVR_FRAMERATE", "15"))
VIDEO_BITRATE = os.getenv("DVR_VIDEO_BITRATE", "1200k")
# `veryfast` tem qualidade/compressão bem melhor que `ultrafast` e, em 640x480,
# o custo de CPU no Celeron N3350 é desprezível.
X264_PRESET_LIVE = os.getenv("DVR_X264_PRESET_LIVE", "veryfast")
X264_PRESET_REC = os.getenv("DVR_X264_PRESET_REC", "veryfast")
# zerolatency reduz a latência do ao vivo mas piora a compressão. Vazio = sem
# tune (arquivos menores/melhor qualidade, ao vivo ~1 s mais lento).
X264_TUNE = os.getenv("DVR_X264_TUNE", "zerolatency").strip()

# --- Filtros de imagem / controles da câmera -------------------------------
# Preset digital aplicado na captura: none|night|ir|bright|sharp|mirror|flip|rotate180
IMAGE_PRESET = os.getenv("DVR_IMAGE_PRESET", "none").strip().lower()
# Cadeia de filtros FFmpeg livre (ex.: "eq=saturation=1.3,unsharp=5:5:1.0"),
# aplicada DEPOIS do preset.
VIDEO_FILTERS = os.getenv("DVR_VIDEO_FILTERS", "").strip()
# Controles v4l2 aplicados com `v4l2-ctl --set-ctrl` no boot
# (ex.: "brightness=128,contrast=32,white_balance_automatic=1").
V4L2_CTRLS = os.getenv("DVR_V4L2_CTRLS", "").strip()

# --- Gravação / retenção ---------------------------------------------------
SEGMENT_SECONDS = int(os.getenv("DVR_SEGMENT_SECONDS", "600"))        # 10 min
RETENTION_SECONDS = int(os.getenv("DVR_RETENTION_SECONDS", str(24 * 3600)))
RETENTION_INTERVAL = int(os.getenv("DVR_RETENTION_INTERVAL", "300"))  # 5 min
MIN_FREE_BYTES = int(os.getenv("DVR_MIN_FREE_BYTES", str(1024 ** 3)))  # 1 GB

# Fonte do gravador. "hls" (padrão) re-muxa o HLS ao vivo com -c copy, o que
# evita abrir /dev/video0 duas vezes (webcams USB só aceitam UM consumidor) e
# evita um segundo encode x264 neste hardware fraco. Use "device" apenas se a
# sua câmera permitir múltiplas aberturas simultâneas.
RECORD_SOURCE = os.getenv("DVR_RECORD_SOURCE", "hls").strip().lower()

# --- Quick Tunnel ----------------------------------------------------------
TUNNEL_TTL = int(os.getenv("DVR_TUNNEL_TTL", "14400"))                # 4 h
TUNNEL_START_TIMEOUT = float(os.getenv("DVR_TUNNEL_START_TIMEOUT", "15"))
TUNNEL_EXPIRY_INTERVAL = float(os.getenv("DVR_TUNNEL_EXPIRY_INTERVAL", "10"))
CLOUDFLARED_BIN = os.getenv("CLOUDFLARED_BIN", "cloudflared")

# --- Servidor --------------------------------------------------------------
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "2310"))
TUNNEL_TARGET = os.getenv("DVR_TUNNEL_TARGET", f"http://localhost:{PORT}")

# Garante que localtime/strftime respeitem o TZ do container (ex.:
# America/Sao_Paulo) — usado tanto pelo FFmpeg quanto pela retenção.
if hasattr(time, "tzset"):  # não existe no Windows, irrelevante em produção
    try:
        time.tzset()
    except Exception:  # pragma: no cover - defensivo
        pass
