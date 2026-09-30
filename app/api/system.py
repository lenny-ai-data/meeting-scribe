import asyncio
import shutil
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends

from .. import db
from ..config import DEVICES, LANGUAGES, WHISPER_MODELS, get_settings
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


async def _ollama() -> dict:
    settings = get_settings()
    info = {"url": settings.ollama_url, "default_model": settings.ollama_model, "reachable": False}
    try:
        async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=5) as client:
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


@router.get("/system", summary="État du service : GPU, Ollama, file d'attente, configuration")
async def system(worker: Annotated[Worker, Depends(get_worker)]):
    settings = get_settings()
    gpu, ollama = await asyncio.gather(_gpu(), _ollama())
    current = worker.current
    return {
        "version": _package_version("meeting-scribe"),
        "gpu": gpu,
        "ollama": ollama,
        "llm_api": {"configured": settings.llm_api_configured, "base_url": settings.llm_api_base_url or None,
                    "model": settings.llm_api_model or None},
        "hf_token": bool(settings.hf_token),
        "auth": bool(settings.api_token),
        "fake_pipeline": settings.fake_pipeline,
        "queue": {
            "running": {"task_id": current["id"], "kind": current["kind"], "job_id": current["job_id"]}
            if current else None,
            "queued": len(db.queued_tasks()),
        },
        "versions": {name: _package_version(name) for name in ("yt-dlp", "whisperx", "torch", "pyannote.audio")},
        "options": {
            "models": WHISPER_MODELS, "languages": LANGUAGES, "devices": DEVICES,
            "default_model": settings.default_model, "default_language": settings.default_language,
            "default_device": settings.default_device,
        },
    }
