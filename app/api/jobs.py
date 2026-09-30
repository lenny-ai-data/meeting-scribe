import shutil
from datetime import datetime
from pathlib import Path, PurePath
from typing import Annotated
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .. import db
from ..config import DEVICES, LANGUAGES, WHISPER_MODELS, get_settings
from ..render import display_title
from ..worker.queue import Worker
from .common import get_worker, job_or_404, link

router = APIRouter(tags=["jobs"])


def job_out(job: dict, detail: bool = False) -> dict:
    base = f"/api/jobs/{job['id']}"
    out = {
        **job,
        "display_title": display_title(job),
        "queue_position": db.queue_position(job["id"]) if job["status"] == "queued" else None,
        "links": {
            "self": link(base),
            "transcript_md": link(f"{base}/transcript.md"),
            "transcript_json": link(f"{base}/transcript.json"),
            "speakers": link(f"{base}/speakers"),
            "audio": link(f"{base}/audio"),
            "ui": link(f"/job.html?id={job['id']}"),
        },
    }
    if detail:
        from .speakers import speakers_out

        out["speakers"] = speakers_out(job)
    return out


def _parse_meeting_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "meeting_date doit être une date ISO 8601 (ex. 2026-10-01T14:30)")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_settings().tz))
    return dt.isoformat(timespec="seconds")


def _check_http_url(value: str, field: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(422, f"{field} doit être une URL http(s)")
    return value.strip()


def _check_choice(value: str | None, default: str, choices: tuple[str, ...], field: str) -> str:
    value = value or default
    if value not in choices:
        raise HTTPException(422, f"{field} doit valoir l'une de ces valeurs : {', '.join(choices)}")
    return value


def _check_speakers(num: int | None, low: int | None, high: int | None) -> tuple:
    for v in (num, low, high):
        if v is not None and not 1 <= v <= 30:
            raise HTTPException(422, "Le nombre d'intervenants doit être compris entre 1 et 30")
    if num is not None:
        return num, None, None
    if low is not None and high is not None and low > high:
        raise HTTPException(422, "min_speakers ne peut pas dépasser max_speakers")
    return None, low, high


def _source_suffix(filename: str) -> str:
    suffix = PurePath(filename).suffix.lower()
    return suffix if suffix[1:].isalnum() and len(suffix) <= 6 else ".bin"


def _save_upload(upload: UploadFile, dst: Path, max_bytes: int) -> int:
    written = 0
    with open(dst, "wb") as out:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                raise HTTPException(413, "Fichier trop volumineux")
            out.write(chunk)
    return written


@router.post("/jobs", status_code=202, summary="Créer un job de transcription (fichier ou URL)")
async def create_job(
    worker: Annotated[Worker, Depends(get_worker)],
    file: Annotated[UploadFile | None, File(description="Fichier audio ou vidéo (.m4a, .mp4…)")] = None,
    url: Annotated[str | None, Form(description="URL à télécharger avec yt-dlp (YouTube…)")] = None,
    model: Annotated[str | None, Form(description="large-v3 ou large-v3-turbo")] = None,
    language: Annotated[str | None, Form(description="fr ou en")] = None,
    device: Annotated[str | None, Form(description="cuda ou cpu")] = None,
    num_speakers: Annotated[int | None, Form()] = None,
    min_speakers: Annotated[int | None, Form()] = None,
    max_speakers: Annotated[int | None, Form()] = None,
    vocabulary: Annotated[str | None, Form(description="Noms propres, jargon : aide la reconnaissance")] = None,
    title: Annotated[str | None, Form()] = None,
    meeting_date: Annotated[str | None, Form(description="Date ISO 8601 ; par défaut celle du fichier")] = None,
    source_name: Annotated[str | None, Form(description="Nom d'origine du fichier (ex. nom sur Drive)")] = None,
    external_ref: Annotated[str | None, Form(description="Identifiant libre renvoyé dans les callbacks")] = None,
    callback_url: Annotated[str | None, Form(description="URL appelée (POST JSON) à la fin du job")] = None,
):
    settings = get_settings()
    url = url.strip() if url else None
    has_file = file is not None and bool(file.filename)
    if has_file == bool(url):
        raise HTTPException(422, "Fournir soit un fichier (file), soit une URL (url)")
    if url:
        url = _check_http_url(url, "url")
    if callback_url:
        callback_url = _check_http_url(callback_url, "callback_url")
    num_speakers, min_speakers, max_speakers = _check_speakers(num_speakers, min_speakers, max_speakers)

    job = db.create_job(
        model=_check_choice(model, settings.default_model, WHISPER_MODELS, "model"),
        language=_check_choice(language, settings.default_language, LANGUAGES, "language"),
        device=_check_choice(device, settings.default_device, DEVICES, "device"),
        num_speakers=num_speakers, min_speakers=min_speakers, max_speakers=max_speakers,
        vocabulary=(vocabulary or "").strip() or None,
        title=(title or "").strip() or None,
        meeting_date=_parse_meeting_date(meeting_date),
        source_name=(source_name or "").strip() or (file.filename if has_file else None),
        source_url=url,
        external_ref=external_ref,
        callback_url=callback_url,
    )
    if has_file:
        job_dir = settings.job_dir(job["id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        source = f"source{_source_suffix(file.filename)}"
        try:
            await run_in_threadpool(_save_upload, file, job_dir / source, settings.max_upload_mb * 1024 * 1024)
        except BaseException:
            shutil.rmtree(job_dir, ignore_errors=True)
            db.delete_job(job["id"])
            raise
        db.update_job(job["id"], source_path=source)

    db.enqueue_task("transcribe", job["id"])
    worker.notify()
    return job_out(db.get_job(job["id"]))


@router.get("/jobs", summary="Lister les jobs (du plus récent au plus ancien)")
def list_jobs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), status: str | None = None):
    jobs, total = db.list_jobs(limit, offset, status)
    return {"items": [job_out(j) for j in jobs], "total": total}


@router.get("/jobs/{job_id}", summary="Détail d'un job (statut, progression, intervenants)")
def get_job(job_id: str):
    return job_out(job_or_404(job_id), detail=True)


@router.post("/jobs/{job_id}/cancel", summary="Annuler un job en attente ou en cours")
async def cancel_job(job_id: str, worker: Annotated[Worker, Depends(get_worker)]):
    job = job_or_404(job_id)
    if job["status"] in db.ACTIVE_JOB_STATUSES:
        await worker.cancel_job(job_id)
        job = db.get_job(job_id)
        if job["status"] in db.ACTIVE_JOB_STATUSES:
            # Tâche retirée de la file avant son démarrage
            db.update_job(job_id, status="cancelled", finished_at=db.now_iso())
    return job_out(db.get_job(job_id))


@router.delete("/jobs/{job_id}", status_code=204, summary="Supprimer un job et ses fichiers")
async def delete_job(job_id: str, worker: Annotated[Worker, Depends(get_worker)]):
    job_or_404(job_id)
    await worker.cancel_job(job_id)
    db.delete_job(job_id)
    shutil.rmtree(get_settings().job_dir(job_id), ignore_errors=True)
    return Response(status_code=204)


@router.get("/jobs/{job_id}/audio", summary="Audio du job (WAV 16 kHz, requêtes Range acceptées)")
def job_audio(job_id: str):
    job_or_404(job_id)
    wav = get_settings().job_dir(job_id) / "audio.wav"
    if not wav.exists():
        raise HTTPException(404, "Audio pas encore préparé")
    return FileResponse(wav, media_type="audio/wav")
