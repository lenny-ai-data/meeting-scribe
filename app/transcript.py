"""Transcript Markdown d'un job terminé, avec les noms d'intervenants à jour."""

from . import db
from .config import get_settings
from .errors import ScribeError
from .jsonio import read_json, write_json
from .media import waveform_peaks
from .render import build_units, render_transcript

TIMELINE_MERGE_GAP = 1.0  # deux passages d'un même intervenant séparés de moins d'1 s forment un seul bloc


def load_segments(job: dict) -> list[dict]:
    path = get_settings().job_dir(job["id"]) / "result.json"
    if not path.exists():
        raise ScribeError("Résultat de transcription introuvable")
    return read_json(path)["segments"]


def transcript_markdown(job: dict) -> str:
    settings = get_settings()
    return render_transcript(job, load_segments(job), db.get_speakers(job["id"]), settings.diarization_model,
                             settings.tz)


def speaker_blocks(segments: list[dict]) -> list[dict]:
    """Passages de parole par intervenant, avec le même recalage que le transcript."""
    blocks: list[dict] = []
    for unit in build_units(segments):
        if not unit.speaker:
            continue
        last = blocks[-1] if blocks else None
        if last and last["speaker"] == unit.speaker and unit.start - last["end"] <= TIMELINE_MERGE_GAP:
            last["end"] = max(last["end"], round(unit.end, 2))
        else:
            blocks.append({"speaker": unit.speaker, "start": round(unit.start, 2), "end": round(unit.end, 2)})
    return blocks


def waveform(job: dict, bins: int) -> list[float] | None:
    """Enveloppe d'amplitude de audio.wav, mise en cache dans waveform.json ; None si le WAV est absent."""
    job_dir = get_settings().job_dir(job["id"])
    wav, cache = job_dir / "audio.wav", job_dir / "waveform.json"
    if not wav.exists():
        return None
    if cache.exists():
        data = read_json(cache)
        if data.get("bins") == bins:
            return data["peaks"]
    peaks = waveform_peaks(wav, bins)
    write_json(cache, {"bins": bins, "peaks": peaks})
    return peaks
