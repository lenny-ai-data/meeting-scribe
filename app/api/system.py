import asyncio
import shutil
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends

from .. import __version__, db
from ..config import LANGUAGES, available_devices, get_settings
from ..profiles import DEFAULT_PROFILE, PROFILES
from ..llm.config import OllamaConfig, llm_config
from ..worker.queue import Worker
from .common import get_worker

router = APIRouter(tags=["system"])


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


async def _gpu() -> list[dict] | None:
    if shutil.which("nvidia-smi") is None:
        return None
    proc = await asyncio.create_subprocess_exec(
        "nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu",
        "--format=csv,noheader,nounits",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    gpus = []
    for line in out.decode().strip().splitlines():
        name, total, used, free, util = [p.strip() for p in line.split(",")]
        gpus.append({"name": name, "memory_total_mb": int(total), "memory_used_mb": int(used),
                     "memory_free_mb": int(free), "utilization_percent": int(util)})
    return gpus


async def _ollama(cfg: OllamaConfig) -> dict:
    info = {"url": cfg.url, "default_model": cfg.model or None, "unload_before_gpu": cfg.unload, "reachable": False}
    try:
        async with httpx.AsyncClient(base_url=cfg.url, timeout=5) as client:
            ver, tags, ps = await asyncio.gather(client.get("/api/version"), client.get("/api/tags"),
                                                 client.get("/api/ps"))
        info.update(
            reachable=True,
            version=ver.json().get("version"),
            models=sorted(m["name"] for m in tags.json().get("models", [])),
            loaded=[{"name": m["name"], "size_vram": m.get("size_vram")} for m in ps.json().get("models", [])],
        )
    except (httpx.HTTPError, ValueError) as exc:
        info["error"] = str(exc) or type(exc).__name__
    return info


def _profiles(settings, gpu: list[dict] | None) -> dict:
    """Profils proposés par appareil ; sur GPU, grisés si la carte n'a pas assez de VRAM au total."""
    total_gb = max((g["memory_total_mb"] for g in gpu), default=0) / 1024 if gpu else None
    out = {}
    for device in available_devices(settings):
        items = []
        for p in PROFILES[device].values():
            item = {"id": p.id, "label": p.label, "model": p.model, "diarization_step": p.diarization_step,
                    "description": p.description, "available": True, "reason": None}
            if device == "cuda":
                need = settings.min_vram_gb(p.model)
                item["min_vram_gb"] = need
                if total_gb is not None and total_gb < need:
                    item["available"] = False
                    item["reason"] = f"{need:g} Go de VRAM requis, carte de {total_gb:.1f} Go".replace(".", ",")
            items.append(item)
        out[device] = items
    return out


def _status(settings, gpu: list[dict] | None, ollama: dict, running: bool) -> dict:
    """Synthèse pour le voyant de l'interface : error (transcription impossible), busy ou ready."""
    problems, warnings = [], []
    if not settings.diarization_ready:
        problems.append("HF_TOKEN manquant et modèle de diarisation non embarqué : la diarisation est impossible")
    if not gpu and settings.default_device == "cuda" and not settings.fake_pipeline:
        problems.append("GPU introuvable (nvidia-smi ne répond pas)")
    if not ollama["reachable"]:
        warnings.append("Ollama injoignable : pas de compte rendu local")
    state = "error" if problems else "busy" if running else "ready"
    return {"state": state, "problems": problems, "warnings": warnings}


@router.get("/system", summary="État du service : GPU, Ollama, file d'attente, configuration")
async def system(worker: Annotated[Worker, Depends(get_worker)]):
    settings = get_settings()
    cfg = llm_config()
    gpu, ollama = await asyncio.gather(_gpu(), _ollama(cfg.ollama))
    current = worker.current
    return {
        "version": __version__,
        "status": _status(settings, gpu, ollama, current is not None),
        "gpu": gpu,
        "ollama": ollama,
        "llm_api": {"configured": cfg.openai.configured, "base_url": cfg.openai.base_url or None,
                    "model": cfg.openai.model or None},
        "hf_token": bool(settings.hf_token),
        "diarization": {"model": settings.diarization_model, "bundled": settings.diarization_bundled},
        "auth": bool(settings.api_token),
        "fake_pipeline": settings.fake_pipeline,
        "queue": {
            "running": {"task_id": current["id"], "kind": current["kind"], "job_id": current["job_id"]}
            if current else None,
            "queued": len(db.queued_tasks()),
        },
        "versions": {name: _package_version(name) for name in ("yt-dlp", "whisperx", "torch", "pyannote.audio")},
        "options": {
            "profiles": _profiles(settings, gpu),
            "default_profiles": {d: settings.default_profile or DEFAULT_PROFILE[d] for d in available_devices(settings)},
            "languages": LANGUAGES, "devices": available_devices(settings),
            "default_language": settings.default_language,
            "default_device": settings.default_device, "owner_name": settings.owner_name or None,
        },
    }
