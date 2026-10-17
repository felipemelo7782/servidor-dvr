"""Quick Tunnel do Cloudflare controlado pela aplicação.

Inicia ``cloudflared tunnel --url http://localhost:2310`` como subprocesso,
captura a URL ``https://<random>.trycloudflare.com`` do stderr e a expõe via
API. O túnel expira automaticamente após TUNNEL_TTL (padrão 4 h).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Literal, Optional

from . import config
from .procutils import terminate_process

log = logging.getLogger("dvr.tunnel")

# cloudflared imprime algo como:
#   "Your quick Tunnel has been created! Visit it at https://abc-def.trycloudflare.com"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

State = Literal["off", "starting", "active", "expired"]


class TunnelManager:
    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._expiry_task: Optional[asyncio.Task] = None
        self._url: Optional[str] = None
        self._expires_at: Optional[float] = None      # epoch (segundos)
        self._state: State = "off"
        self._lock = asyncio.Lock()
        self._url_ready = asyncio.Event()

    # ------------------------------------------------------------------ API
    async def start(self) -> dict:
        async with self._lock:
            if (self._state in ("active", "starting")
                    and self._proc is not None and self._proc.returncode is None):
                return self.status()

            self._reset()
            self._state = "starting"
            self._url_ready.clear()

            cmd = [
                config.CLOUDFLARED_BIN, "tunnel",
                "--url", config.TUNNEL_TARGET,
                "--no-autoupdate",
            ]
            log.info("Iniciando túnel: %s", " ".join(cmd))
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                log.error("cloudflared não encontrado no PATH")
                self._state = "off"
                return self.status()

            self._reader_task = asyncio.create_task(self._read_output(self._proc), name="tunnel-reader")
            self._expiry_task = asyncio.create_task(self._watch_expiry(), name="tunnel-expiry")

        # Espera a URL aparecer (o cloudflared leva alguns segundos).
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._url_ready.wait(), timeout=config.TUNNEL_START_TIMEOUT)

        if self._url:
            log.info("Túnel ativo em %s (expira em %s s)", self._url, config.TUNNEL_TTL)
        else:
            log.warning("Túnel iniciado, mas a URL não apareceu em %.0fs",
                        config.TUNNEL_START_TIMEOUT)
        return self.status()

    async def stop(self, state: State = "off") -> dict:
        async with self._lock:
            current = asyncio.current_task()
            for task in (self._reader_task, self._expiry_task):
                if task and task is not current:
                    task.cancel()
            self._reader_task = self._expiry_task = None
            await self._teardown(state)
        return self.status()

    def status(self) -> dict:
        now = time.time()
        active = (
            self._state == "active"
            and self._proc is not None
            and self._proc.returncode is None
        )
        remaining = 0
        if active and self._expires_at is not None:
            remaining = max(0, int(self._expires_at - now))
        return {
            "state": self._state,
            "active": active,
            "url": self._url if active else None,
            "expires_at": (
                datetime.fromtimestamp(self._expires_at, tz=timezone.utc).isoformat()
                if active and self._expires_at else None
            ),
            "remaining_seconds": remaining,
            "ttl_seconds": config.TUNNEL_TTL,
        }

    # ------------------------------------------------------------- internos
    def _reset(self, state: State = "off") -> None:
        self._proc = None
        self._url = None
        self._expires_at = None
        self._state = state

    async def _teardown(self, state: State) -> None:
        """Encerra o processo e limpa o estado. NÃO mexe nas tasks.

        Mantido separado de ``stop`` para poder ser chamado de dentro da própria
        task de expiração sem autocancelamento.
        """
        proc = self._proc
        was_active = self._state in ("active", "starting")
        await terminate_process(proc, send_quit=False)
        if state == "expired":
            log.info("Túnel expirado (TTL de %s s) e encerrado", config.TUNNEL_TTL)
        elif was_active:
            log.info("Túnel encerrado")
        self._reset(state=state)

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        """Lê stdout/stderr à procura da URL do quick tunnel."""
        async def consume(stream) -> None:
            if stream is None:
                return
            async for raw in stream:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue
                log.debug("[cloudflared] %s", line)
                if self._url is None:
                    match = _URL_RE.search(line)
                    if match:
                        self._url = match.group(0)
                        self._expires_at = time.time() + config.TUNNEL_TTL
                        self._state = "active"
                        self._url_ready.set()
                        log.info("URL do túnel capturada: %s", self._url)

        await asyncio.gather(consume(proc.stderr), consume(proc.stdout))

        # Processo terminou sozinho enquanto "ativo" => limpa o estado (a menos
        # que stop()/expiração já tenha tratado isso).
        if self._state in ("active", "starting"):
            log.warning("cloudflared encerrou inesperadamente")
            self._reset(state="off")

    async def _watch_expiry(self) -> None:
        """Encerra o túnel quando o TTL de 4 h é atingido."""
        while True:
            await asyncio.sleep(config.TUNNEL_EXPIRY_INTERVAL)
            if self._state != "active" or self._expires_at is None:
                # Nada ativo: se o processo morreu, encerra a task.
                if self._proc is None or self._proc.returncode is not None:
                    return
                continue
            if time.time() >= self._expires_at:
                async with self._lock:
                    still_expired = (
                        self._state == "active"
                        and self._expires_at is not None
                        and time.time() >= self._expires_at
                    )
                    if still_expired:
                        self._expiry_task = None   # evita autocancelamento
                        await self._teardown("expired")
                return
