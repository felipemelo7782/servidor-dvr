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

O preset também pode ser trocado em tempo de execução pela interface web
(``POST /api/camera/preset``); esse estado vive em memória e volta ao valor de
``DVR_IMAGE_PRESET`` quando o container reinicia.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import config

log = logging.getLogger("dvr.imaging")

# Presets prontos. O valor é uma cadeia de filtros FFmpeg.
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

# --- Estado em tempo de execução -------------------------------------------
_runtime_preset: str = config.IMAGE_PRESET if config.IMAGE_PRESET in PRESETS else "none"
_runtime_filters: str = config.VIDEO_FILTERS


def available_presets() -> list[str]:
    return list(PRESETS)


def current_preset() -> str:
    return _runtime_preset


def current_filters() -> str:
    return _runtime_filters


def set_preset(preset: str, filters: str | None = None) -> None:
    """Troca o preset ativo (e, opcionalmente, os filtros livres)."""
    global _runtime_preset, _runtime_filters
    preset = (preset or "none").strip().lower()
    if preset not in PRESETS:
        raise ValueError(f"preset inválido: {preset!r}")
    _runtime_preset = preset
    if filters is not None:
        _runtime_filters = filters.strip()


def compose_chain(preset: str, filters: str | None) -> str | None:
    """Monta a cadeia ``-vf`` para um preset/filtros quaisquer (sem aplicar)."""
    parts: list[str] = []
    preset_filters = PRESETS.get((preset or "none").strip().lower())
    if preset_filters:
        parts.append(preset_filters)
    if filters and filters.strip():
        parts.append(filters.strip())
    return ",".join(parts) or None


def build_filter_chain() -> str | None:
    """Cadeia ``-vf`` atual (preset + filtros livres) ou ``None``."""
    return compose_chain(_runtime_preset, _runtime_filters)


# --- Execução de comandos auxiliares ---------------------------------------
async def _run(args: list[str], timeout: float = 8.0) -> str | None:
    """Executa um comando curto e devolve a saída combinada; None se indisponível."""
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


async def validate_chain(chain: str) -> bool:
    """Testa a cadeia ``-vf`` com um vídeo sintético.

    Evita que um filtro inválido derrube o stream ao vivo: se a validação falhar,
    o chamador deve cair de volta para ``none``.
    """
    if not chain:
        return True
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10",
            "-vf", chain, "-frames:v", "3", "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return True  # sem ffmpeg não há como validar; não bloqueia
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    if proc.returncode != 0:
        log.error("Cadeia de filtros inválida (%s): %s",
                  chain, err.decode(errors="replace").strip()[:500])
        return False
    return True


async def startup() -> None:
    """Logue os controles disponíveis, aplique os configurados e valide filtros."""
    listing = await list_controls()
    if listing is None:
        log.info("v4l2-ctl indisponível ou câmera ausente; sem controles V4L2")
    else:
        log.info("Controles da câmera disponíveis:\n%s", listing.strip())
    await apply_controls()

    chain = build_filter_chain()
    if chain and not await validate_chain(chain):
        log.warning("Desativando filtros de imagem: a cadeia não é válida neste FFmpeg")
        set_preset("none", "")
        chain = None
    log.info("Filtros de imagem: preset=%s, -vf=%s",
             current_preset(), chain or "(nenhum)")
