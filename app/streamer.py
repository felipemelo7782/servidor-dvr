"""Pipeline HLS ao vivo.

No modo USB, um único processo FFmpeg persistente lê /dev/video0 e escreve
segmentos HLS em ``HLS_DIR/stream.m3u8``. Nos modos IP (icsee/yoosee) o mesmo
processo lê a câmera via RTSP — o restante do pipeline é idêntico. Um watchdog
reinicia o processo se ele morrer (câmera desconectada, erro de driver, etc.).

Este é o ÚNICO processo que abre a câmera. O gravador (recorder.py) consome o
HLS gerado aqui em vez de abrir /dev/video0 de novo — webcams USB (uvcvideo)
normalmente só permitem um consumidor por vez (e com RTSP o remux do HLS segue
mais simples e estável).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from . import config, imaging, probe
from .procutils import terminate_process

log = logging.getLogger("dvr.streamer")

RESTART_DELAY = 3.0        # espera entre tentativas de reinício
MAX_RESTART_DELAY = 30.0   # teto do backoff (evita loop apertado que trava a CPU)
QUICK_EXIT = 10.0          # se morrer em menos de X s, dobra o backoff
PROBE_AFTER = 3            # mortes rápidas seguidas antes de chamar o auto-probe
# Se a câmera IP parar de responder, o FFmpeg fica preso na leitura TCP. Este
# timeout de I/O (10 s, microssegundos) faz ele morrer e o watchdog reiniciar.
IPCAM_RW_TIMEOUT_US = "10000000"

# Dica exibida UMA vez por processo quando o FFmpeg não consegue abrir a câmera.
IPCAM_HINT = ("modo IP: 'Invalid data'/'Connection refused' na câmera costuma "
              "significar RTSP desligado ou porta/canal errados — no "
              "Xiongmai/iCSee o RTSP fica na 554 (a 34567 é o protocolo do "
              "app), o canal do RTSP começa em 1 e existe a chave 'RTSP "
              "server' no app da câmera. Verifique também o IP/host.")


class HlsStreamer:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._watchdog: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._running = False
        self._started_at: float = 0.0
        self._probed = False    # auto-probe já tentado nesta vida do processo?
        self._hinted = False    # dica de diagnóstico (IPCAM_HINT) já exibida?
        self.last_stderr: Optional[str] = None  # última linha do FFmpeg (diagnóstico)

    def command(self) -> list[str]:
        """Despacha o comando FFmpeg conforme o modo de operação."""
        if config.MODO_OP == "usb":
            return self._usb_command()
        return self._ipcam_command()

    def _usb_command(self) -> list[str]:
        """Monta o comando FFmpeg do HLS ao vivo (webcam USB).

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

    def _ipcam_command(self) -> list[str]:
        """Monta o comando FFmpeg do HLS ao vivo (câmera IP icsee/yoosee).

        A câmera já entrega H.264, então por padrão o stream é apenas COPIADO
        para o HLS (-c copy): no Celeron N3350 o re-encode é caro e desnecessá-
        rio. Se houver preset de filtro configurado (ou DVR_IPCAM_TRANSCODE=1),
        re-encode com os filtros — assim a gravação (remux do HLS) continua
        herdando exatamente a mesma imagem.
        """
        url = config.ipcam_url()
        transport = config.IPCAM_TRANSPORT
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
        if transport == "tcp":
            # TCP: sem artefatos de rede em Wi-Fi/PLC (UDP perde pacotes).
            cmd += ["-rtsp_transport", "tcp"]
        cmd += ["-rw_timeout", IPCAM_RW_TIMEOUT_US, "-i", url, "-an"]

        tr = config.IPCAM_TRANSCODE
        filters = imaging.build_filter_chain()
        transcode = tr in {"1", "true", "yes", "on", "force"} or (
            filters is not None and tr not in {"0", "false", "no", "off"}
        )
        if transcode:
            if filters:
                cmd += ["-vf", filters]
            cmd += ["-c:v", "libx264", "-preset", config.X264_PRESET_LIVE]
            if config.X264_TUNE:
                cmd += ["-tune", config.X264_TUNE]
            # Sem -s/-r de propósito: a resolução/fps vem da câmera (impor 640x480
            # escondido rebaixaria a imagem do usuário). O bitrate continua o knob
            # DVR_VIDEO_BITRATE para manter a consistência com o modo USB.
            cmd += [
                "-b:v", config.VIDEO_BITRATE,
                "-g", str(max(1, config.FRAMERATE * 2)),
                "-pix_fmt", "yuv420p",
            ]
        else:
            cmd += ["-c:v", "copy"]

        cmd += [
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
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
        if config.MODO_OP == "usb":
            if not os.path.exists(config.VIDEO_DEVICE):
                log.error("Dispositivo %s ausente; stream HLS não iniciado",
                          config.VIDEO_DEVICE)
                self._proc = None
                return
        else:
            if not config.ipcam_url():
                log.error("Câmera IP não configurada (defina DVR_IPCAM_HOST); "
                          "stream HLS não iniciado (modo %s)", config.MODO_OP)
                self._proc = None
                return
            log.info("Fonte ao vivo: %s", config.ipcam_url_masked())
        # Cancela o leitor de stderr anterior (se houver) antes de abrir outro.
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None
        cmd = self.command()
        # O comando pode conter a URL RTSP com credenciais: SEMPRE logar mascarado.
        log.info("Iniciando FFmpeg HLS: %s", config.mask_secrets(" ".join(cmd)))
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

        As linhas do FFmpeg podem ECOAR a URL RTSP de erro com senha (ex.:
        "Invalid data found ...: rtsp://user:senha@host/..."). Mascarar antes de
        logar e antes de expor em self.last_stderr (/healthz).
        """
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self.last_stderr = config.mask_secrets(line)
                log.info("[hls/ffmpeg] %s", self.last_stderr)
                if (config.MODO_OP != "usb" and not self._hinted
                        and ("nvalid data" in line or "onnection" in line)):
                    self._hinted = True
                    log.warning("Diagnóstico: %s", IPCAM_HINT)

    async def _guard(self) -> None:
        """Reinicia o FFmpeg quando ele morre, com backoff exponencial.

        O backoff evita que uma falha persistente (ex.: filtro inválido, câmera
        ocupada) gere um loop de reinício a cada 3 s que consome a CPU inteira de
        um Celeron. Nos modos IP, 3 mortes rápidas em sequência disparam o
        auto-probe de porta/canal (probe.run_probe) — uma tentativa por boot.
        """
        delay = RESTART_DELAY
        consec_quick = 0  # mortes rápidas seguidas (disparam o auto-probe)
        while self._running:
            await asyncio.sleep(delay)
            if not self._running:
                break
            proc = self._proc
            if proc is None or proc.returncode is not None:
                lived = time.monotonic() - self._started_at if self._started_at else 0.0
                died_quick = lived < QUICK_EXIT
                delay = (min(delay * 2, MAX_RESTART_DELAY) if died_quick
                         else RESTART_DELAY)
                consec_quick = consec_quick + 1 if died_quick else 0
                code = None if proc is None else proc.returncode
                log.warning("FFmpeg HLS encerrou (código=%s, viveu %.1fs); "
                            "reiniciando em %.0fs", code, lived, delay)
                if config.MODO_OP != "usb" and not self._probed \
                        and consec_quick >= PROBE_AFTER:
                    self._probed = True
                    log.info("FFmpeg falhou %d vezes seguidas: tentando "
                             "auto-probe de porta/canal da câmera",
                             consec_quick)
                    if await probe.run_probe():
                        # Override registrado: logos abaixo já apontam p/ nova URL.
                        delay = RESTART_DELAY
                await self._spawn()
            else:
                delay = RESTART_DELAY
                consec_quick = 0

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
