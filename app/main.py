"""Aplicação FastAPI: rotas, autenticação e ciclo de vida dos subsistemas.

Um único worker uvicorn é obrigatório: o estado (streamer, gravador, túnel) vive
no processo.
"""
from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .recorder import Recorder, _segment_start
from .streamer import HlsStreamer
from .tunnel import TunnelManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dvr.main")

streamer = HlsStreamer()
recorder = Recorder()
tunnel = TunnelManager()

# MIME types que o StaticFiles do Starlette nem sempre acerta para HLS.
_HLS_MIME = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.HLS_DIR.mkdir(parents=True, exist_ok=True)
    config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    if config.DVR_PASSWORD == "changeme":
        log.warning("DVR_PASSWORD não definido — usando o valor padrão 'changeme'!")
    log.info("Fonte de gravação: %s", config.RECORD_SOURCE)

    await streamer.start()
    await recorder.start()
    log.info("DVR pronto em http://%s:%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        log.info("Encerrando DVR…")
        await tunnel.stop()
        await recorder.stop()
        await streamer.stop()


app = FastAPI(title="DVR Yoosee-like", lifespan=lifespan)


# --------------------------------------------------------------- autenticação
def _authorized(header: str | None) -> bool:
    """Valida o cabeçalho HTTP Basic contra DVR_USER/DVR_PASSWORD."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1], validate=True).decode()
    except Exception:
        return False
    user, _, password = raw.partition(":")
    return (
        secrets.compare_digest(user, config.DVR_USER)
        and secrets.compare_digest(password, config.DVR_PASSWORD)
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # /hls/ fica público de propósito: o player (hls.js/Safari) não envia
    # credenciais e o conteúdo é apenas o stream da câmera.
    if path.startswith("/hls/") or path == "/healthz":
        return await call_next(request)
    if not _authorized(request.headers.get("authorization")):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="DVR", charset="UTF-8"'},
        )
    return await call_next(request)


# ------------------------------------------------------------------ páginas
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


@app.get("/tunnel")
async def tunnel_page() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "tunnel.html")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "stream": streamer._proc is not None and streamer._proc.returncode is None,
        "recording": recorder._proc is not None and recorder._proc.returncode is None,
        "tunnel": tunnel.status()["state"],
    }


# --------------------------------------------------------------- gravações
@app.get("/api/recordings")
async def list_recordings() -> list[dict]:
    items: list[dict] = []
    for path in sorted(config.RECORDINGS_DIR.glob("*.mp4"), key=_segment_start, reverse=True):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({
            "filename": path.name,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "started_at": _segment_start(path).isoformat(),
            "url": f"/api/recordings/{path.name}",
        })
    return items


@app.get("/api/recordings/{filename}")
async def download_recording(filename: str) -> FileResponse:
    # Bloqueia path traversal e extensões inesperadas.
    if filename != Path(filename).name or not filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido")
    target = config.RECORDINGS_DIR / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Gravação não encontrada")
    return FileResponse(target, media_type="video/mp4", filename=filename)


# --------------------------------------------------------------- HLS público
@app.get("/hls/{path:path}")
async def hls_file(path: str) -> FileResponse:
    root = config.HLS_DIR.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Não encontrado")
    suffix = target.suffix.lower()
    mime = _HLS_MIME.get(suffix, "application/octet-stream")
    # A playlist muda a cada ~2 s: não pode ser cacheada.
    headers = {"Cache-Control": "no-cache" if suffix == ".m3u8" else "max-age=10"}
    return FileResponse(target, media_type=mime, headers=headers)


# ------------------------------------------------------------------ túnel
@app.post("/api/tunnel/start")
async def tunnel_start() -> dict:
    return await tunnel.start()


@app.post("/api/tunnel/stop")
async def tunnel_stop() -> dict:
    return await tunnel.stop()


@app.get("/api/tunnel/status")
async def tunnel_status() -> dict:
    return tunnel.status()


# --------------------------------------------------------------- estáticos
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
