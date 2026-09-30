from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import db
from ..config import get_settings
from ..llm.summarize import render_summary
from ..render import suggested_filename
from ..worker.queue import Worker
from .common import get_worker, job_or_404, link, require_completed
from .speakers import content_disposition

router = APIRouter(tags=["summaries"])


class SummaryRequest(BaseModel):
    prompt_id: str | None = Field(None, description="Prompt système stocké ; le prompt par défaut sinon")
    system_prompt: str | None = Field(None, description="Texte de prompt système ponctuel (remplace prompt_id)")
    meeting_prompt: str = Field("", description="Consignes propres à cette réunion")
    provider: Literal["ollama", "openai"] = "ollama"
    model: str | None = Field(None, description="Par défaut OLLAMA_MODEL ou LLM_API_MODEL")
    think: bool = Field(False, description="Mode « thinking » (Ollama, modèles compatibles)")
    temperature: float | None = Field(None, ge=0, le=2)


def summary_out(summary: dict) -> dict:
    base = f"/api/summaries/{summary['id']}"
    return {**summary, "think": bool(summary["think"]),
            "links": {"self": link(base), "markdown": link(f"{base}.md")}}


def summary_or_404(summary_id: str) -> dict:
    summary = db.get_summary(summary_id)
    if summary is None:
        raise HTTPException(404, "Compte rendu introuvable")
    return summary


@router.post("/jobs/{job_id}/summaries", status_code=202, summary="Générer un compte rendu par LLM")
def create_summary(job_id: str, body: SummaryRequest, worker: Annotated[Worker, Depends(get_worker)]):
    settings = get_settings()
    job = job_or_404(job_id)
    require_completed(job)
    if body.provider == "openai" and not settings.llm_api_configured:
        raise HTTPException(400, "API LLM non configurée (LLM_API_BASE_URL, LLM_API_MODEL dans .env)")

    if body.system_prompt and body.system_prompt.strip():
        prompt_id, prompt_name, system_prompt = None, "(ponctuel)", body.system_prompt
    else:
        prompt = db.get_prompt(body.prompt_id) if body.prompt_id else db.get_default_prompt()
        if prompt is None:
            raise HTTPException(404, "Prompt introuvable")
        prompt_id, prompt_name, system_prompt = prompt["id"], prompt["name"], prompt["content"]

    default_model = settings.ollama_model if body.provider == "ollama" else settings.llm_api_model
    summary = db.create_summary(
        job_id=job_id, provider=body.provider, model=body.model or default_model,
        prompt_id=prompt_id, prompt_name=prompt_name, system_prompt=system_prompt,
        meeting_prompt=body.meeting_prompt, think=int(body.think), temperature=body.temperature,
    )
    db.enqueue_task("summarize", job_id, summary_id=summary["id"])
    worker.notify()
    return summary_out(summary)


@router.get("/jobs/{job_id}/summaries", summary="Comptes rendus d'un job (le plus récent en premier)")
def list_summaries(job_id: str):
    job_or_404(job_id)
    return [summary_out(s) for s in db.list_summaries(job_id)]


@router.get("/summaries/{summary_id}.md", summary="Compte rendu en Markdown", response_class=Response,
            responses={200: {"content": {"text/markdown": {}}}})
def summary_md(summary_id: str, download: bool = Query(False)):
    summary = summary_or_404(summary_id)
    if summary["status"] != "completed":
        raise HTTPException(409, f"Compte rendu non terminé (statut : {summary['status']})")
    job = db.get_job(summary["job_id"])
    return Response(
        render_summary(summary, job),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition(suggested_filename(job, "_compte-rendu"), download)},
    )


@router.get("/summaries/{summary_id}", summary="Détail d'un compte rendu")
def get_summary(summary_id: str):
    return summary_out(summary_or_404(summary_id))


@router.post("/summaries/{summary_id}/cancel", summary="Annuler un compte rendu en attente ou en cours")
async def cancel_summary(summary_id: str, worker: Annotated[Worker, Depends(get_worker)]):
    summary_or_404(summary_id)
    await worker.cancel_summary(summary_id)
    return summary_out(db.get_summary(summary_id))


@router.delete("/summaries/{summary_id}", status_code=204, summary="Supprimer un compte rendu")
async def delete_summary(summary_id: str, worker: Annotated[Worker, Depends(get_worker)]):
    summary_or_404(summary_id)
    await worker.cancel_summary(summary_id)
    db.delete_summary(summary_id)
    return Response(status_code=204)
