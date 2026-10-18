"""Pipeline HLS ao vivo.

Um único processo FFmpeg persistente lê /dev/video0 e escreve segmentos HLS em
``HLS_DIR/stream.m3u8``. Um watchdog reinicia o processo se ele morrer (câmera
desconectada, erro de driver, etc.).

Este é o ÚNICO processo que abre a câmera. O gravador (recorder.py) consome o
HLS gerado aqui em vez de abrir /dev/video0 de novo — webcams USB (uvcvideo)
normalmente só permitem um consumidor por vez.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from . import config, imaging
from .procutils import terminate_process

log = logging.getLogger("dvr.streamer")

RESTART_DELAY = 3.0        # espera entre tentativas de reinício
MAX_RESTART_DELAY = 30.0   # teto do backoff (evita loop apertado que trava a CPU)
QUICK_EXIT = 10.0          # se morrer em menos de X s, dobra o backoff


class HlsStreamer:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._running = False
        self._started_at: float = 0.0
        self.last_stderr: Optional[str] = None  # última linha do FFmpeg (diagnóstico)

    def command(self) -> list[str]:
        """Monta o comando FFmpeg do HLS ao vivo.

        Os filtros de imagem (preset + DVR_VIDEO_FILTERS) entram aqui. Como a
        gravação é um remux deste HLS, ela herda os mesmos filtros sem custo
        extra de CPU.
        """
        fmt = ["-input_format", config.INPUT_FORMAT] if config.INPUT_FORMAT else []
        gop = max(1, config.FRAMERATE * 2)  # ~1 keyframe a cada 2 s (hls_time)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-f", "v4l2", *fmt,
            "-framerate", str(config.FRAMERATE),
            "-video_size", config.VIDEO_SIZE,
            "-i", config.VIDEO_DEVICE,
            "-an",
        ]
        filters = imaging.build_filter_chain()
        if filters:
            cmd += ["-vf", filters]
        cmd += [
            "-c:v", "libx264",
            "-preset", config.X264_PRESET_LIVE,
        ]
        if config.X264_TUNE:
            cmd += ["-tune", config.X264_TUNE]
        cmd += [
            "-r", str(config.FRAMERATE),
            "-s", config.VIDEO_SIZE,
            "-b:v", config.VIDEO_BITRATE,
            "-g", str(gop),
            # yuv420p garante reprodução em qualquer navegador/player.
            "-pix_fmt", "yuv420p",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            # delete_segments: mantém a playlist curta (baixo uso de disco).
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", str(config.HLS_DIR / "seg_%03d.ts"),
            str(config.HLS_DIR / "stream.m3u8"),
        ]
        return cmd

    async def start(self) -> None:
        config.HLS_DIR.mkdir(parents=True, exist_ok=True)
        if self._running:
            return
        self._running = True
        await self._spawn()
        self._watchdog = asyncio.create_task(self._guard(), name="hls-watchdog")

    async def _spawn(self) -> None:
        if not os.path.exists(config.VIDEO_DEVICE):
            log.error("Dispositivo %s ausente; stream HLS não iniciado", config.VIDEO_DEVICE)
            self._proc = None
            return
        # Cancela o leitor de stderr anterior (se houver) antes de abrir outro.
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        cmd = self.command()
        log.info("Iniciando FFmpeg HLS: %s", " ".join(cmd))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("ffmpeg não encontrado no PATH; stream HLS indisponível")
            self._proc = None
            return
        self.last_stderr = None
        self._started_at = time.monotonic()
        self._stderr_task = asyncio.create_task(self._drain(self._proc), name="hls-stderr")

    async def _drain(self, proc: asyncio.subprocess.Process) -> None:
        """Consome o stderr do FFmpeg.

        Logado em INFO (não DEBUG): com ``-loglevel warning`` o volume é baixo e,
        quando algo quebra, é exatamente aqui que aparece o motivo.
        """
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.last_stderr = line
                log.info("[hls/ffmpeg] %s", line)

    async def _guard(self) -> None:
        """Reinicia o FFmpeg quando ele morre, com backoff exponencial.

        O backoff evita que uma falha persistente (ex.: filtro inválido, câmera
        ocupada) gere um loop de reinício a cada 3 s que consome a CPU inteira de
        um Celeron.
        """
        delay = RESTART_DELAY
        while self._running:
            await asyncio.sleep(delay)
            if not self._running:
                break
            proc = self._proc
            if proc is None or proc.returncode is not None:
                lived = time.monotonic() - self._started_at if self._started_at else 0.0
                delay = min(delay * 2, MAX_RESTART_DELAY) if lived < QUICK_EXIT else RESTART_DELAY
                code = None if proc is None else proc.returncode
                log.warning("FFmpeg HLS encerrou (código=%s, viveu %.1fs); "
                            "reiniciando em %.0fs", code, lived, delay)
                await self._spawn()
            else:
                delay = RESTART_DELAY

    async def restart(self) -> None:
        """Reinicia o FFmpeg (usado ao trocar filtros em tempo de execução)."""
        was_running = self._running
        await self.stop()
        if was_running:
            await self.start()

    async def stop(self) -> None:
        self._running = False
        for task in (self._watchdog, self._stderr_task):
            if task:
                task.cancel()
        self._watchdog = self._stderr_task = None
        # Saída HLS não precisa de finalização; SIGTERM basta.
        await terminate_process(self._proc, send_quit=False)
        self._proc = None
        log.info("FFmpeg HLS parado")
