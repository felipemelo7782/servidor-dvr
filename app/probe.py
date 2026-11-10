"""Auto-probe da câmera IP (só nos modos icsee/yoosee; USB nunca passa por aqui).

Quando o FFmpeg do streamer morre repetidas vezes no boot (porta errada, canal
errado, RTSP desabilitado), este módulo testa uma lista CURTA de combinações
porta/canal com ``ffprobe`` (mesma máquina, mesma rede — o teste é idêntico ao
live) e devolve a primeira que responde. A URL vencedora é registrada em
``config.set_ipcam_override`` (memória) e o streamer reaparece com ela.

Regras de ouro:
- Nunca roda no modo usb e é ignorado se DVR_IPCAM_URL foi definido (aí o usuário
  já declarou a URL) ou com DVR_AUTO_PROBE=0.
- Não loga credenciais: toda saída passa por ``config.mask_secrets``.
- Estado é efêmero: um redeploy (container novo) volta a respeitar o env.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil

from . import config

log = logging.getLogger("dvr.probe")

FFPROBE_TIMEOUT = 9.0      # por tentativa (rw_timeout interno ~5 s manda o teto)
URL_SLICE_LOG = 160        # truncado do erro de cada tentativa (mascarado)

# Timeout de I/O do ffprobe em microssegundos (mesma mecânica do streamer).
_RW_TIMEOUT_US = "5000000"


def _candidates() -> list[tuple[int, int]]:
    """Combinações (porta, canal) a testar, na ordem.

    - A combinação do env vai PRIMEIRA (respeita o usuário).
    - Depois os valores "oficiais" do Xiongmai: RTSP na 554, canal 1-based;
      por fim variações com a porta do app (34567) e canal 0, caso o firmware
      da câmera use RTSP na porta do app ou canal 0-based (variantes existem).
    """
    if config.MODO_OP == "yoosee":
        # Yoosee não tem canal; só a porta pode variar.
        out = [(config.IPCAM_PORT, config.IPCAM_CHANNEL)]
        if config.IPCAM_PORT != 554:
            out.append((554, config.IPCAM_CHANNEL))
        return out

    base = (config.IPCAM_PORT, config.IPCAM_CHANNEL)
    out = [base]
    for cand in ((554, config.IPCAM_CHANNEL), (config.IPCAM_PORT, 1),
                 (554, 1), (554, 0), (34567, 1)):
        if cand not in out:
            out.append(cand)
    return out[:6]


def _accepts_probe() -> tuple[bool, str]:
    """Guardas do probe. Retorna (ok, motivo-quando-recusa)."""
    if config.MODO_OP == "usb":
        return False, "modo usb não usa probe"
    if not config.AUTO_PROBE:
        return False, "DVR_AUTO_PROBE=0"
    if config.IPCAM_URL_OVERRIDE:
        return False, "DVR_IPCAM_URL definido (URL manual tem prioridade)"
    if not config.IPCAM_HOST:
        return False, "câmera IP não configurada"
    if not shutil.which("ffprobe"):
        return False, "ffprobe não encontrado"
    return True, ""


async def run_probe() -> bool:
    """Testa as combinações; se achar uma válida, registra override e True."""
    ok, reason = _accepts_probe()
    if not ok:
        log.info("auto-probe desativado: %s", reason)
        return False

    combos = _candidates()
    log.info("auto-probe: testando %d combinação(ões) porta/canal via ffprobe: %s",
             len(combos), [f"{p}/{c}" for p, c in combos])

    for port, channel in combos:
        url = config.ipcam_url_for(port, channel)
        if not url:
            continue
        cmd = ["ffprobe", "-v", "error", "-hide_banner", "-nostdin"]
        if config.IPCAM_TRANSPORT == "tcp":
            cmd += ["-rtsp_transport", "tcp"]
        cmd += [
            "-rw_timeout", _RW_TIMEOUT_US,
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "csv=p=0",
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.warning("auto-probe: ffprobe sumiu do PATH; abortando")
            return False

        try:
            out, err = await asyncio.wait_for(proc.communicate(), FFPROBE_TIMEOUT)
        except asyncio.TimeoutError:
            # ffprobe preso (sem resposta): mata para não acumular processos
            # no Celeron (os watchdogs ficam de fora do caminho do probe).
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            log.info("auto-probe: %s/%s sem resposta em %.0fs", port, channel,
                     FFPROBE_TIMEOUT)
            continue

        if proc.returncode == 0 and out:
            codec = out.decode(errors="replace").strip() or "vídeo"
            config.set_ipcam_override(url)
            log.info("auto-probe: RTSP ABRIU em porta %s canal %s (%s); "
                     "usando %s", port, channel, codec, config.mask_secrets(url))
            return True

        detail = (err.decode(errors="replace").strip() or
                  f"exit={proc.returncode}")[:URL_SLICE_LOG]
        log.info("auto-probe: porta %s canal %s falhou: %s",
                 port, channel, config.mask_secrets(detail))

    log.warning(
        "auto-probe: nenhuma combinação respondeu. Se a câmera for Xiongmai/"
        "iCSee, verifique no app: chave 'RTSP server' LIGADA, e se o aparelho "
        "for cloud-only (sem RTSP), a saída é uma ponte dvrip:// -> RTSP "
        "ex.: https://github.com/AlexxIT/go2rtc")
    return False
