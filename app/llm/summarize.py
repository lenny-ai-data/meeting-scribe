"""Génération des comptes rendus (tâche `summarize` de la file unique)."""

import logging

from .. import db
from ..config import get_settings
from ..render import display_title, frontmatter, now_local
from ..transcript import transcript_markdown
from . import ollama, openai_compat
from .base import LLMError

log = logging.getLogger("llm.summarize")

PROVIDERS = ("ollama", "openai")


def build_user_message(meeting_prompt: str, transcript: str) -> str:
    parts = []
    if meeting_prompt.strip():
        parts.append("## Consignes propres à cette réunion\n\n" + meeting_prompt.strip())
    parts.append("## Transcription\n\n" + transcript)
    return "\n\n".join(parts)


async def run_summary_task(task: dict) -> None:
    settings = get_settings()
    summary = db.get_summary(task["summary_id"])
    job = db.get_job(task["job_id"])
    db.update_summary(summary["id"], status="running", error=None)
    user = build_user_message(summary["meeting_prompt"], transcript_markdown(job))

    if summary["provider"] == "ollama":
        result = await ollama.complete(
            settings.ollama_url, summary["model"], summary["system_prompt"], user,
            think=bool(summary["think"]), max_ctx=settings.ollama_max_ctx, timeout=settings.llm_timeout,
            temperature=summary["temperature"],
        )
    elif summary["provider"] == "openai":
        if not settings.llm_api_configured:
            raise LLMError("API LLM non configurée (LLM_API_BASE_URL, LLM_API_MODEL)")
        result = await openai_compat.complete(
            settings.llm_api_base_url, settings.llm_api_key, summary["model"], summary["system_prompt"], user,
            timeout=settings.llm_timeout, temperature=summary["temperature"],
        )
    else:
        raise LLMError(f"Fournisseur inconnu : {summary['provider']}")

    if not result.content:
        raise LLMError("Le modèle a renvoyé une réponse vide")
    db.update_summary(summary["id"], status="completed", content=result.content, model=result.model,
                      prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                      finished_at=db.now_iso())
    log.info("Compte rendu %s terminé (%s tokens en entrée)", summary["id"], result.prompt_tokens)


def render_summary(summary: dict, job: dict) -> str:
    header = {
        "title": f"Compte rendu — {display_title(job)}",
        "date": job.get("meeting_date"),
        "job_id": job["id"],
        "summary_id": summary["id"],
        "source_file": job.get("source_name"),
        "provider": summary["provider"],
        "model": summary["model"],
        "prompt": summary["prompt_name"],
        "generated_at": now_local(get_settings().tz),
    }
    return frontmatter(header) + "\n" + (summary["content"] or "").strip() + "\n"
