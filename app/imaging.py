"""Filtros de imagem e controles da câmera.

Duas alavancas diferentes:

1. **Filtros FFmpeg** (``-vf``): pós-processamento digital (brilho, contraste,
   gamma, nitidez, preto-e-branco/"IR", espelhar). Não dependem da câmera e são
   aplicados no encoder do HLS — como a gravação é um remux do HLS, ela herda os
   mesmos filtros automaticamente.

2. **Controles V4L2** (``v4l2-ctl``): ajustes de hardware expostos pela webcam
   (brilho, exposição, balanço de branco, ganho e, em alguns modelos, controles
   relacionados ao filtro de corte de infravermelho). Dependem do que o driver
   expõe — use ``/api/camera/controls`` para listar o que a sua câmera aceita.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import config

log = logging.getLogger("dvr.imaging")

# Presets prontos para DVR_IMAGE_PRESET. O valor é uma cadeia de filtros FFmpeg.
PRESETS: dict[str, str] = {
    "none": "",
    # Noturno: clareia e reduz o ruído típico de cenas escuras.
    "night": "hqdn3d=2:1:2:3,eq=brightness=0.10:contrast=1.10:gamma=1.25",
    # Infravermelho: alto contraste, sem cor (como câmera com IR ligado) + denoise.
    "ir": "hqdn3d=2:1:2:3,eq=brightness=0.12:contrast=1.25:gamma=1.30:saturation=0",
    "bright": "eq=brightness=0.12:contrast=1.05",
    "sharp": "unsharp=5:5:1.0:5:5:0.0",
    "mirror": "hflip",
    "flip": "vflip",
    "rotate180": "hflip,vflip",
}


def build_filter_chain() -> str | None:
    """Monta o valor de ``-vf`` (preset + filtros livres) ou ``None``."""
    parts: list[str] = []
    preset = PRESETS.get(config.IMAGE_PRESET)
    if preset:
        parts.append(preset)
    elif config.IMAGE_PRESET not in (None, "", "none"):
        log.warning("DVR_IMAGE_PRESET=%r desconhecido; presets válidos: %s",
                    config.IMAGE_PRESET, ", ".join(PRESETS))
    if config.VIDEO_FILTERS:
        parts.append(config.VIDEO_FILTERS)
    return ",".join(p for p in parts if p) or None


async def _run(args: list[str], timeout: float = 8.0) -> str | None:
    """Executa um comando curto e devolve a saída; None se indisponível."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    return out.decode(errors="replace")


async def list_controls() -> str | None:
    """Lista os controles V4L2 que a câmera expõe (brilho, exposição, IR…)."""
    if not os.path.exists(config.VIDEO_DEVICE):
        return None
    return await _run(["v4l2-ctl", "-d", config.VIDEO_DEVICE, "--list-ctrls"])


async def apply_controls() -> None:
    """Aplica DVR_V4L2_CTRLS via ``v4l2-ctl --set-ctrl``."""
    if not config.V4L2_CTRLS:
        return
    if not os.path.exists(config.VIDEO_DEVICE):
        log.warning("DVR_V4L2_CTRLS definido, mas %s não existe", config.VIDEO_DEVICE)
        return
    result = await _run(
        ["v4l2-ctl", "-d", config.VIDEO_DEVICE, "--set-ctrl", config.V4L2_CTRLS]
    )
    if result is None:
        log.warning("v4l2-ctl indisponível; controles não aplicados")
    else:
        log.info("Controles V4L2 aplicados (%s): %s",
                 config.V4L2_CTRLS, (result or "").strip() or "ok")


async def startup() -> None:
    """Logue os controles disponíveis e aplique os configurados."""
    listing = await list_controls()
    if listing is None:
        log.info("v4l2-ctl indisponível ou câmera ausente; sem controles V4L2")
    else:
        log.info("Controles da câmera disponíveis:\n%s", listing.strip())
    await apply_controls()

    chain = build_filter_chain()
    log.info("Filtros de imagem: preset=%s, -vf=%s",
             config.IMAGE_PRESET, chain or "(nenhum)")
