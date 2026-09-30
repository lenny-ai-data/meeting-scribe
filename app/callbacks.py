"""Webhooks sortants (n8n…) : POST JSON sur callback_url à chaque événement d'un job.

Événements : job.completed, job.failed, job.speakers_updated, summary.completed.
Le Markdown est inclus dans le message pour éviter une seconde requête.
"""

import asyncio
import logging

import httpx

from . import db
from .config import get_settings
from .render import display_title, suggested_filename

log = logging.getLogger("callbacks")

RETRY_DELAYS = (5, 15, 45)  # secondes entre les tentatives (4 au total)


def _link(path: str) -> str:
    return get_settings().public_base_url.rstrip("/") + path


def build_payload(event: str, job: dict, summary: dict | None = None) -> dict:
    from .api.speakers import speakers_out
    from .transcript import transcript_markdown

    base = f"/api/jobs/{job['id']}"
    payload = {
        "event": event,
        "job_id": job["id"],
        "status": job["status"],
        "external_ref": job["external_ref"],
        "source_name": job["source_name"],
        "source_url": job["source_url"],
        "title": display_title(job),
        "meeting_date": job["meeting_date"],
        "duration_seconds": round(job["duration"]) if job["duration"] else None,
        "language": job["language"],
        "model": job["model"],
        "error": job["error"],
        "links": {
            "job": _link(base),
            "transcript_md": _link(f"{base}/transcript.md"),
            "transcript_json": _link(f"{base}/transcript.json"),
            "ui": _link(f"/job.html?id={job['id']}"),
        },
    }
    if job["status"] == "completed":
        payload["speakers"] = [{"id": s["id"], "name": s["display_name"], "talk_time": s["talk_time"]}
                               for s in speakers_out(job)]
        payload["suggested_filename"] = suggested_filename(job)
        payload["markdown"] = transcript_markdown(job)
    if summary is not None:
        from .llm.summarize import render_summary

        payload["summary"] = {
            "id": summary["id"],
            "provider": summary["provider"],
            "model": summary["model"],
            "prompt": summary["prompt_name"],
            "suggested_filename": suggested_filename(job, "_compte-rendu"),
            "markdown": render_summary(summary, job),
            "link": _link(f"/api/summaries/{summary['id']}.md"),
        }
    return payload


async def send_job_event(event: str, job_id: str, summary_id: str | None = None) -> None:
    job = db.get_job(job_id)
    if job is None or not job["callback_url"]:
        return
    summary = db.get_summary(summary_id) if summary_id else None
    try:
        payload = build_payload(event, job, summary)
    except Exception:
        log.exception("Construction du callback %s impossible", event)
        return
    headers = {"X-Scribe-Event": event}
    if token := get_settings().callback_token:
        headers["X-Scribe-Token"] = token

    error = ""
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt, delay in enumerate((0, *RETRY_DELAYS), 1):
            await asyncio.sleep(delay)
            try:
                resp = await client.post(job["callback_url"], json=payload, headers=headers)
                if resp.status_code < 400:
                    db.update_job(job_id, callback_status=f"{event} : HTTP {resp.status_code} ({db.now_iso()})")
                    return
                error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                error = str(exc) or type(exc).__name__
            log.warning("Callback %s du job %s, tentative %d : %s", event, job_id, attempt, error)
    db.update_job(job_id, callback_status=f"{event} : échec après {len(RETRY_DELAYS) + 1} tentatives ({error})")


async def on_task_done(task: dict, status: str) -> None:
    """Branché sur le worker : traduit la fin d'une tâche en événement."""
    if task["kind"] == "summarize":
        summary = db.get_summary(task["summary_id"]) if task["summary_id"] else None
        if summary and summary["status"] == "completed":
            await send_job_event("summary.completed", task["job_id"], summary["id"])
        return
    job = db.get_job(task["job_id"])
    if job is None:
        return
    if task["kind"] == "transcribe":
        if job["status"] == "completed":
            await send_job_event("job.completed", job["id"])
        elif job["status"] == "failed":
            await send_job_event("job.failed", job["id"])
    elif task["kind"] == "rediarize" and status == "done":
        await send_job_event("job.speakers_updated", job["id"])
