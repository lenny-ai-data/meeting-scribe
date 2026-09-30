from fastapi import HTTPException, Request

from .. import db
from ..config import get_settings
from ..worker.queue import Worker


def get_worker(request: Request) -> Worker:
    return request.app.state.worker


def link(path: str) -> str:
    """Lien vers une ressource de l'API, absolu si PUBLIC_BASE_URL est défini."""
    return get_settings().public_base_url.rstrip("/") + path


def job_or_404(job_id: str) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job introuvable")
    return job


def require_completed(job: dict) -> None:
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail=f"Le job n'est pas terminé (statut : {job['status']})")
