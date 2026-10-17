"""Utilidades compartilhadas para encerrar subprocessos FFmpeg/cloudflared."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("dvr.proc")


async def terminate_process(proc, *, timeout: float = 8.0, send_quit: bool = False) -> None:
    """Encerra um subprocesso asyncio de forma graciosa.

    ``send_quit=True`` escreve ``q`` no stdin — o jeito documentado de fazer o
    FFmpeg finalizar o arquivo de saída (escrever o moov do MP4) antes de sair.
    Só funciona se o processo foi criado com ``stdin=PIPE``.
    """
    if proc is None or proc.returncode is not None:
        return

    if send_quit and proc.stdin is not None:
        try:
            proc.stdin.write(b"q")
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    else:
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        log.warning("Processo %s não encerrou em %.0fs; enviando SIGKILL", proc.pid, timeout)

    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()
