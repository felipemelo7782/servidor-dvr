"""Configuração central.

Toda a parametrização vem de variáveis de ambiente e é lida SOMENTE aqui, para
manter um único ponto de verdade sobre caminhos e limites. O restante do código
importa este módulo em vez de chamar ``os.getenv`` diretamente.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import quote


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

# --- Modo de operação -------------------------------------------------------
# Escolhe a FONTE do vídeo (uma câmera por vez):
#   usb    -> webcam USB em VIDEO_DEVICE (v4l2) — comportamento original.
#   icsee  -> câmera IP via RTSP "estilo Xiongmai" (RTSP na 554; a 34567 é o
#             protocolo do app iCSee/Xmeye — DVRIP — e NÃO é RTSP).
#   yoosee -> câmera IP via RTSP, fluxos onvif1/onvif2 do Yoosee (porta 554).
# Os modos IP seguem o MESMO pipeline do USB: só muda a entrada do FFmpeg no
# streamer; o gravador continua remuxando o HLS local, idêntico na prática.
_MODO_ALIASES = {"xmeye": "icsee", "icsee": "icsee", "yoosee": "yoosee", "usb": "usb"}
_MODO_RAW = os.getenv("DVR_MODO_OP", os.getenv("MODO_OP", "usb")).strip().lower()
MODO_OP = _MODO_ALIASES.get(_MODO_RAW, "usb")

# Porta RTSP padrão por modo. No Xiongmai/iCSee o RTSP vive na porta clássica
# 554 (a 34567 é o "media/DVRIP port" do app, que não fala RTSP); Yoosee idem.
# Sempre sobrescrevível com DVR_IPCAM_PORT.
_DEFAULT_IPCAM_PORT = {"icsee": 554, "yoosee": 554}

# --- Câmera IP (usada só quando MODO_OP != "usb") ---------------------------
# DVR_IPCAM_HOST aceita "192.168.1.10", "192.168.1.10:34567" ou "rtsp://ip:554".


def _parse_host(raw: str) -> tuple[str, int | None]:
    """Normaliza DVR_IPCAM_HOST: separa host puro de uma porta embutida."""
    text = raw.strip()
    if "://" in text:  # usuário colou a URL completa no HOST
        text = text.split("://", 1)[1]
    host, sep, port = text.partition(":")
    host = host.rstrip("/")
    if sep:
        try:
            return host, int(port)
        except ValueError:
            pass
    return (host or "").strip(), None


_host_raw = os.getenv("DVR_IPCAM_HOST", "")
IPCAM_HOST, _EMBEDDED_PORT = _parse_host(_host_raw)
IPCAM_PORT = int(os.getenv("DVR_IPCAM_PORT", "") or _EMBEDDED_PORT
                 or _DEFAULT_IPCAM_PORT.get(MODO_OP) or 554)
IPCAM_USER = os.getenv("DVR_IPCAM_USER", "admin").strip() or "admin"
IPCAM_PASS = os.getenv("DVR_IPCAM_PASS", "").strip()
IPCAM_CHANNEL = int(os.getenv("DVR_IPCAM_CHANNEL", "") or "1")   # canal NVR/1ª câmera
IPCAM_STREAM = int(os.getenv("DVR_IPCAM_STREAM", "") or "0")     # 0=principal 1=sub
# tcp = confiável em Wi-Fi/PLC (recomendado); udp pode ter menos latência.
IPCAM_TRANSPORT = os.getenv("DVR_IPCAM_TRANSPORT", "tcp").strip().lower()
# auto  = copia o H.264 da câmera (-c copy); re-encode SÓ se houver preset de
#         filtro configurado (assim a gravação segue herdando os filtros).
# 1     = força re-encode sempre (mais pesado no Celeron).
# 0     = nunca re-encode (filtros DIGITAIS ficam desativados neste modo).
IPCAM_TRANSCODE = os.getenv("DVR_IPCAM_TRANSCODE", "auto").strip().lower()
# URL RTSP completa que sobrescreve tudo acima (válvula de escape para
# firmwares exóticos; NELA o canal também é 1). Ex.: rtsp://admin:senha@ip:554/user=admin&password=senha&channel=1&stream=0.sdp?real_stream
IPCAM_URL_OVERRIDE = os.getenv("DVR_IPCAM_URL", "").strip()
# DVR_AUTO_PROBE (modos IP): após 3 falhas rápidas seguidas do FFmpeg, testa
# combinações porta/canal conhecidas com ffprobe e usa a primeira que abrir.
# DVR_IPCAM_URL definido desliga o efeito (o usuário já decidiu a URL).
AUTO_PROBE = _env_bool("DVR_AUTO_PROBE", True)
# URL vencedora do auto-probe: vive SÓ em memória (estilo o preset de imagem) e
# morre com o container — a próxima reinicialização volta a respeitar o env.
_ipcam_override: str | None = None

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


# ----------------------------------------------------- URL RTSP da câmera IP
def set_ipcam_override(url: str | None) -> None:
    """Define/limpa a URL vencedora do auto-probe (estado em memória).

    Só tem efeito quando DVR_IPCAM_URL não está definido — a variável de env
    é SEMPRE a última palavra (o probe não derruba uma escolha manual).
    """
    global _ipcam_override
    _ipcam_override = url


def ipcam_url() -> str | None:
    """Monta a URL RTSP da câmera IP conforme o modo; None se não configurada.

    Precedência: DVR_IPCAM_URL (env, verbatim) > URL do auto-probe (memória) >
    URL montada das DVR_IPCAM_*:
      icsee/xmeye -> rtsp://user:pass@host:554/user=U&password=P&channel=1&stream=0.sdp?real_stream
                     (RTSP do Xiongmai; o canal é 1-based — 34567 é o port do
                      app/DVRIP e não aparece aqui)
      yoosee      -> rtsp://user:pass@host:554/onvif1  (onvif2 = fluxo secundário)
    """
    if IPCAM_URL_OVERRIDE:
        return IPCAM_URL_OVERRIDE
    if _ipcam_override:
        return _ipcam_override
    if MODO_OP == "usb" or not IPCAM_HOST:
        return None
    return ipcam_url_for(IPCAM_PORT, IPCAM_CHANNEL)


def ipcam_url_for(port: int, channel: int) -> str | None:
    """Monta a URL RTSP para uma porta/canal específicos (usada pelo auto-probe).

    Ignora ambos os overrides — sempre variação canônica do env. No modo yoosee
    o canal é ignorado (o streams são onvif1/onvif2, caminho fixo). None fora
    dos modos IP.
    """
    if MODO_OP == "usb" or not IPCAM_HOST:
        return None
    user = quote(IPCAM_USER, safe="")
    pwd = quote(IPCAM_PASS, safe="")
    if MODO_OP == "yoosee":
        return f"rtsp://{user}:{pwd}@{IPCAM_HOST}:{port}/onvif{IPCAM_STREAM + 1}"
    # icsee (também aceito por iCSee "puro" e clones Xmeye): credenciais no
    # userinfo E na query — firmwares às vezes exigem só um dos dois.
    return (f"rtsp://{user}:{pwd}@{IPCAM_HOST}:{port}/"
            f"user={user}&password={pwd}&channel={channel}"
            f"&stream={IPCAM_STREAM}.sdp?real_stream")


def mask_secrets(text: str | None) -> str | None:
    """Mascara credenciais RTSP em QUALQUER texto (comando, URL, log)."""
    if not text:
        return text
    # 1) Senha conhecida (DVR_IPCAM_PASS): mascara TODAS as ocorrências
    #    (userinfo do yoosee = 1x; icsee tem 1x no userinfo e 1x na query).
    if IPCAM_PASS:
        encoded = quote(IPCAM_PASS, safe="")
        if encoded:
            text = text.replace(encoded, "***")
    # 2) Credenciais no userinfo não cobertas (URL custom com outra senha).
    #    Funciona em URL pura OU dentro de uma linha de comando inteira.
    head, sep, rest = text.partition("://")
    creds, at, tail = rest.partition("@")
    user, colon, pwd = creds.partition(":")
    if at and colon and pwd and "@" not in user:
        # Sanity: não mascarar um "user@" sem senha ou conteúdo aleatório.
        text = f"{head}://{user}:***@{tail}"
    # 3) Qualquer "password=..." que sobrar na query (icsee), conhecido ou não.
    return re.sub(r"(password=)([^&\s]+)", r"\1***", text, flags=re.IGNORECASE)


def ipcam_url_masked() -> str | None:
    """URL RTSP com a senha mascarada (para log e API de diagnóstico)."""
    return mask_secrets(ipcam_url()) if ipcam_url() else None


def camera_label() -> str:
    """Rótulo curto da câmera ativa (topbar/healthz), sem segredos."""
    if MODO_OP == "usb":
        return VIDEO_DEVICE
    label = f"{MODO_OP} · {IPCAM_HOST or '?'}:{IPCAM_PORT}"
    if IPCAM_URL_OVERRIDE:
        label += " (URL manual)"
    elif _ipcam_override:
        label += " (auto-probe)"
    return label
