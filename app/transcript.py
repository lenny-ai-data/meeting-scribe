"""Transcript Markdown d'un job terminé, avec les noms d'intervenants à jour."""

from . import db
from .config import get_settings
from .errors import ScribeError
from .jsonio import read_json
from .render import render_transcript


def load_segments(job: dict) -> list[dict]:
    path = get_settings().job_dir(job["id"]) / "result.json"
    if not path.exists():
        raise ScribeError("Résultat de transcription introuvable")
    return read_json(path)["segments"]


def transcript_markdown(job: dict) -> str:
    settings = get_settings()
    return render_transcript(job, load_segments(job), db.get_speakers(job["id"]), settings.diarization_model,
                             settings.tz)
