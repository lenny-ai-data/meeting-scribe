from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..jsonio import read_json
from ..render import render_transcript, speaker_names, suggested_filename
from ..speakers import default_label, sample_path
from ..worker.queue import Worker
from .common import get_worker, job_or_404, link, require_completed

router = APIRouter(tags=["speakers"])


def speakers_out(job: dict) -> list[dict]:
    speakers = db.get_speakers(job["id"])
    total = sum(sp["talk_time"] for sp in speakers) or 1.0
    base = f"/api/jobs/{job['id']}/speakers"
    return [
        {
            "id": sp["id"],
            "name": sp["name"],
            "display_name": sp["name"] or default_label(sp["id"], job["language"]),
            "talk_time": sp["talk_time"],
            "share": round(sp["talk_time"] / total, 3),
            "first_start": sp["first_start"],
            "samples": [{**s, "url": link(f"{base}/{sp['id']}/samples/{s['n']}.mp3")} for s in sp["samples"]],
        }
        for sp in speakers
    ]


@router.get("/jobs/{job_id}/speakers", summary="Intervenants détectés, avec temps de parole et extraits")
def list_speakers(job_id: str):
    return speakers_out(job_or_404(job_id))


@router.get("/jobs/{job_id}/speakers/{speaker_id}/samples/{n}.mp3", summary="Extrait audio d'un intervenant",
            response_class=FileResponse)
def speaker_sample(job_id: str, speaker_id: str, n: int):
    job_or_404(job_id)
    path = sample_path(get_settings().job_dir(job_id), speaker_id, n)
    if not path.is_file():
        raise HTTPException(404, "Extrait introuvable")
    return FileResponse(path, media_type="audio/mpeg")


@router.put("/jobs/{job_id}/speakers", summary="Nommer les intervenants",
            description='Ex. `{"S1": "Alice", "S2": "Bob", "S3": "Alice"}`. Deux intervenants sous le même nom '
                        "sont fusionnés dans le transcript. Une valeur vide ou null rétablit le libellé par défaut.")
async def rename_speakers(
    job_id: str,
    names: Annotated[dict[str, str | None], Body(examples=[{"S1": "Alice", "S2": "Bob"}])],
):
    job = job_or_404(job_id)
    known = {sp["id"] for sp in db.get_speakers(job_id)}
    unknown = set(names) - known
    if unknown:
        raise HTTPException(422, f"Intervenant(s) inconnu(s) : {', '.join(sorted(unknown))}")
    cleaned = {sid: (" ".join(name.split()) or None) if name else None for sid, name in names.items()}
    db.set_speaker_names(job_id, cleaned)
    return speakers_out(job)


class RediarizeRequest(BaseModel):
    num_speakers: int | None = Field(None, ge=1, le=30, description="Nombre exact d'intervenants")
    min_speakers: int | None = Field(None, ge=1, le=30)
    max_speakers: int | None = Field(None, ge=1, le=30)


@router.post("/jobs/{job_id}/rediarize", status_code=202,
             summary="Relancer seulement la diarisation (ex. avec un nombre d'intervenants imposé)")
def rediarize(job_id: str, body: RediarizeRequest, worker: Annotated[Worker, Depends(get_worker)]):
    job = job_or_404(job_id)
    require_completed(job)
    if not (get_settings().job_dir(job_id) / "aligned.json").exists():
        raise HTTPException(409, "Transcription alignée introuvable pour ce job")
    num = body.num_speakers
    db.update_job(job_id, status="queued", progress=0, error=None, num_speakers=num,
                  min_speakers=None if num else body.min_speakers, max_speakers=None if num else body.max_speakers)
    db.enqueue_task("rediarize", job_id)
    worker.notify()
    from .jobs import job_out

    return job_out(db.get_job(job_id))


@router.get("/people", tags=["speakers"], summary="Noms déjà attribués (autocomplétion)")
def people():
    return db.list_people()


# --- Transcript -----------------------------------------------------------------------

def load_segments(job: dict) -> list[dict]:
    path = get_settings().job_dir(job["id"]) / "result.json"
    if not path.exists():
        raise HTTPException(409, "Résultat de transcription introuvable")
    return read_json(path)["segments"]


def transcript_markdown(job: dict) -> str:
    settings = get_settings()
    return render_transcript(job, load_segments(job), db.get_speakers(job["id"]), settings.diarization_model,
                             settings.tz)


def content_disposition(filename: str, download: bool) -> str:
    ascii_name = filename.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
    kind = "attachment" if download else "inline"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


@router.get("/jobs/{job_id}/transcript.md", tags=["transcript"], summary="Transcript Markdown (noms à jour)",
            response_class=Response, responses={200: {"content": {"text/markdown": {}}}})
def transcript_md(job_id: str, download: bool = Query(False, description="Forcer le téléchargement")):
    job = job_or_404(job_id)
    require_completed(job)
    return Response(
        transcript_markdown(job),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition(suggested_filename(job), download)},
    )


@router.get("/jobs/{job_id}/transcript.json", tags=["transcript"], summary="Segments et mots horodatés")
def transcript_json(job_id: str):
    job = job_or_404(job_id)
    require_completed(job)
    speakers = db.get_speakers(job_id)
    return {
        "job_id": job_id,
        "language": job["language"],
        "duration": job["duration"],
        "speakers": [{"id": sid, "name": name} for sid, name in speaker_names(job, speakers).items()],
        "segments": load_segments(job),
    }
