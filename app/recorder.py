"""Gravação contínua em segmentos de 10 min + retenção de 24 h.

Um processo FFmpeg escreve arquivos MP4 nomeados por data/hora
(``YYYY-MM-DD_HH-MM-SS.mp4``, via ``-strftime 1``). Uma task asyncio periódica
remove os segmentos mais antigos até o total caber na janela de retenção,
protegendo SEMPRE o segmento ativo (o mais recente, que ainda está sendo
gravado).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Optional

from . import config
from .procutils import terminate_process

log = logging.getLogger("dvr.recorder")

# Nome gerado por "-strftime 1". A retenção depende DESTE formato; se mudar o
# padrão em command(), atualize o regex abaixo.
_NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4$")

RESTART_DELAY = 3.0   # reinício do FFmpeg se ele morrer
BOOT_GRACE = 5.0      # espera o HLS existir antes da 1ª checagem de retenção
DELETE_GRACE = 60.0   # nunca apagar arquivos tocados nos últimos N s


def _segment_start(path) -> datetime:
    """Início do segmento: pelo nome do arquivo (confiável) ou mtime (fallback)."""
    m = _NAME_RE.match(path.name)
    if m:
        try:
            return datetime(*(int(g) for g in m.groups()))
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime)


class Recorder:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._retention_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------ FFmpeg
    def command(self) -> list[str]:
        """Monta o comando FFmpeg de gravação por segmentos."""
        if config.RECORD_SOURCE == "device":
            fmt = ["-input_format", config.INPUT_FORMAT] if config.INPUT_FORMAT else []
            inputs = [
                "-f", "v4l2", *fmt,
                "-framerate", str(config.FRAMERATE),
                "-video_size", config.VIDEO_SIZE,
                "-i", config.VIDEO_DEVICE,
            ]
            venc = [
                "-c:v", "libx264", "-preset", config.X264_PRESET_REC,
                "-r", str(config.FRAMERATE),
                "-s", config.VIDEO_SIZE,
                "-b:v", config.VIDEO_BITRATE,
            ]
        else:
            # Loopback: lê o HLS local e apenas remuxa (-c copy) para MP4.
            # Opções -reconnect* cobrem quedas/restart do stream ao vivo.
            inputs = [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-i", f"http://localhost:{config.PORT}/hls/stream.m3u8",
            ]
            venc = ["-c", "copy"]

        # NÃO usar -nostdin aqui: o encerramento gracioso abaixo envia "q" pelo
        # stdin para o FFmpeg escrever o moov do MP4 antes de sair.
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            *inputs,
            "-an",
            *venc,
            "-f", "segment",
            "-segment_time", str(config.SEGMENT_SECONDS),
            "-reset_timestamps", "1",
            "-segment_format", "mp4",
            "-strftime", "1",
            str(config.RECORDINGS_DIR / "%Y-%m-%d_%H-%M-%S.mp4"),
        ]

    async def start(self) -> None:
        config.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        if self._running:
            return
        self._running = True
        await self._spawn()
        self._watchdog = asyncio.create_task(self._guard(), name="rec-watchdog")
        self._retention_task = asyncio.create_task(self._retention_loop(), name="rec-retention")

    async def _spawn(self) -> None:
        if config.RECORD_SOURCE == "device" and not os.path.exists(config.VIDEO_DEVICE):
            log.error("Dispositivo %s ausente; gravação não iniciada", config.VIDEO_DEVICE)
            self._proc = None
            return
        cmd = self.command()
        log.info("Iniciando FFmpeg gravação: %s", " ".join(cmd))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,      # necessário para o "q" gracioso
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("ffmpeg não encontrado no PATH; gravação indisponível")
            self._proc = None
            return
        self._stderr_task = asyncio.create_task(self._drain(self._proc), name="rec-stderr")

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.debug("[rec/ffmpeg] %s", line)

    async def _guard(self) -> None:
        while self._running:
            await asyncio.sleep(RESTART_DELAY)
            if not self._running:
                break
            if self._proc is None or self._proc.returncode is not None:
                code = None if self._proc is None else self._proc.returncode
                log.warning("FFmpeg gravação encerrou (código=%s); reiniciando", code)
                await self._spawn()

    async def stop(self) -> None:
        self._running = False
        for task in (self._watchdog, self._stderr_task, self._retention_task):
            if task:
                task.cancel()
        self._watchdog = self._stderr_task = self._retention_task = None
        # Gracioso: finaliza o MP4 em andamento.
        await terminate_process(self._proc, send_quit=True)
        self._proc = None
        log.info("FFmpeg gravação parado")

    # --------------------------------------------------------------- Retenção
    async def _retention_loop(self) -> None:
        """Roda uma vez no boot (limpa backlog) e depois periodicamente."""
        await asyncio.sleep(BOOT_GRACE)
        while self._running:
            try:
                self.enforce_retention()
            except Exception:  # nunca deixar a task morrer silenciosamente
                log.exception("Falha na rotina de retenção")
            await asyncio.sleep(config.RETENTION_INTERVAL)

    def enforce_retention(self) -> None:
        """Apaga os segmentos mais antigos até o total caber na janela de 24 h.

        A duração de cada segmento é estimada pelo intervalo entre os timestamps
        dos nomes de arquivo (limitado a SEGMENT_SECONDS), o que evita chamar
        ffprobe em cada arquivo.
        """
        files = sorted(config.RECORDINGS_DIR.glob("*.mp4"), key=_segment_start)
        if len(files) <= 1:
            self._check_free_space()
            return

        now = datetime.now()
        starts = [_segment_start(f) for f in files]

        # Duração estimada de cada segmento.
        durations: list[float] = []
        for i, start in enumerate(starts):
            if i < len(starts) - 1:
                span = (starts[i + 1] - start).total_seconds()
            else:
                span = (now - start).total_seconds()   # segmento ativo (parcial)
            durations.append(max(0.0, min(span, config.SEGMENT_SECONDS)))

        total = sum(durations)
        active = files[-1]                 # nunca apagar: está sendo gravado

        # 1) Respeita a janela de retenção.
        deleted = 0
        i = 0
        while total > config.RETENTION_SECONDS and i < len(files) - 1:
            path = files[i]
            if path == active:
                i += 1
                continue
            total -= self._try_delete(path, durations[i])
            deleted += 1
            i += 1

        if deleted:
            log.info("Retenção: %d segmento(s) apagado(s); total agora %.2f h",
                     deleted, total / 3600)

        # 2) Garante espaço livre mínimo em disco.
        self._check_free_space()

    def _check_free_space(self) -> None:
        """Se o disco estiver abaixo do mínimo, apaga mais antigos (protege o ativo)."""
        try:
            free = shutil.disk_usage(config.RECORDINGS_DIR).free
        except OSError:
            return
        if free >= config.MIN_FREE_BYTES:
            return

        log.warning("Disco baixo: %.2f GB livres; removendo gravações antigas",
                    free / 1e9)
        files = sorted(config.RECORDINGS_DIR.glob("*.mp4"), key=_segment_start)
        for path in files[:-1]:  # mantém o segmento ativo
            if free >= config.MIN_FREE_BYTES:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                free += size
                log.info("Retenção (disco): apagado %s", path.name)
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.warning("Não foi possível apagar %s: %s", path.name, exc)

    @staticmethod
    def _try_delete(path, duration: float) -> float:
        """Apaga um arquivo; retorna a duração a descontar do total."""
        # Salvaguarda: não apagar arquivos muito recentes (grace period).
        # Retorna 0.0 para NÃO descontar a duração do total (o arquivo continua
        # no disco e será reavaliado na próxima passada).
        try:
            age = datetime.now().timestamp() - path.stat().st_mtime
        except OSError:
            return duration
        if age < DELETE_GRACE:
            return 0.0

        try:
            size = path.stat().st_size
            path.unlink()
            log.info("Retenção: apagado %s (%.1f MB)", path.name, size / 1e6)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Falha ao apagar %s: %s", path.name, exc)
            return 0.0
        return duration
