"""Rendu du transcript Markdown : en-tête YAML puis tours de parole horodatés, regroupés par intervenant."""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePath
from zoneinfo import ZoneInfo

import yaml

from .speakers import default_label

PARAGRAPH_GAP = 3.0    # pause (s) qui ouvre un nouveau paragraphe dans un même tour
PARAGRAPH_MAX = 90.0   # durée (s) au-delà de laquelle un paragraphe est coupé


@dataclass
class Unit:
    speaker: str | None
    start: float
    end: float
    text: str


@dataclass
class Paragraph:
    start: float
    end: float
    texts: list[str] = field(default_factory=list)


@dataclass
class Turn:
    name: str
    paragraphs: list[Paragraph]


def fmt_ts(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _smooth(runs: list[list[dict]]) -> list[list[dict]]:
    """Rattache un mot isolé attribué à un autre intervenant au milieu d'une même voix
    (bruit de diarisation), mais garde les vraies interjections de plusieurs mots."""
    out: list[list[dict]] = []
    i = 0
    while i < len(runs):
        run = runs[i]
        if (out and i + 1 < len(runs) and len(run) == 1
                and out[-1][0]["speaker"] == runs[i + 1][0]["speaker"] != run[0]["speaker"]):
            out[-1].extend(run + runs[i + 1])
            i += 2
            continue
        if out and out[-1][0]["speaker"] == run[0]["speaker"]:
            out[-1].extend(run)
        else:
            out.append(list(run))
        i += 1
    return out


def build_units(segments: list[dict]) -> list[Unit]:
    """Découpe les segments Whisper aux changements d'intervenant détectés au niveau des mots."""
    units = []
    for seg in segments:
        words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
        if not any("start" in w for w in words):
            if seg.get("text", "").strip():
                units.append(Unit(seg.get("speaker"), seg["start"], seg["end"], seg["text"].strip()))
            continue
        # Mots sans horodatage (nombres, sigles) ou sans intervenant : hérités du mot précédent
        speaker, t = seg.get("speaker"), seg["start"]
        filled = []
        for w in words:
            speaker = w.get("speaker") or speaker
            t_start = w.get("start", t)
            t = w.get("end", t_start)
            filled.append({"word": w["word"].strip(), "start": t_start, "end": t, "speaker": speaker})
        runs: list[list[dict]] = []
        for w in filled:
            if runs and runs[-1][0]["speaker"] == w["speaker"]:
                runs[-1].append(w)
            else:
                runs.append([w])
        for run in _smooth(runs):
            units.append(Unit(run[0]["speaker"], run[0]["start"], run[-1]["end"], " ".join(w["word"] for w in run)))
    return units


def build_turns(segments: list[dict], names: dict[str, str], language: str) -> list[Turn]:
    turns: list[Turn] = []
    for unit in build_units(segments):
        name = names.get(unit.speaker or "") or default_label(unit.speaker, language)
        if turns and turns[-1].name == name:
            para = turns[-1].paragraphs[-1]
            if unit.start - para.end > PARAGRAPH_GAP or unit.end - para.start > PARAGRAPH_MAX:
                turns[-1].paragraphs.append(Paragraph(unit.start, unit.end, [unit.text]))
            else:
                para.texts.append(unit.text)
                para.end = max(para.end, unit.end)
        else:
            turns.append(Turn(name, [Paragraph(unit.start, unit.end, [unit.text])]))
    return turns


def display_title(job: dict) -> str:
    if job.get("title"):
        return job["title"]
    if job.get("source_name"):
        return PurePath(job["source_name"]).stem
    return job.get("source_url") or job["id"]


def speaker_names(job: dict, speakers: list[dict]) -> dict[str, str]:
    return {sp["id"]: sp["name"] or default_label(sp["id"], job["language"]) for sp in speakers}


def now_local(tz: str) -> str:
    return datetime.now(timezone.utc).astimezone(ZoneInfo(tz)).isoformat(timespec="seconds")


def frontmatter(data: dict) -> str:
    return "---\n" + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000) + "---\n"


def render_transcript(job: dict, segments: list[dict], speakers: list[dict], diarization_model: str,
                      tz: str = "Europe/Paris") -> str:
    names = speaker_names(job, speakers)
    turns = build_turns(segments, names, job["language"])
    speaking = list(dict.fromkeys(t.name for t in turns))
    duration = job.get("duration") or 0
    header = {
        "title": display_title(job),
        "date": job.get("meeting_date"),
        "duration": fmt_ts(duration),
        "duration_seconds": round(duration),
        "language": job["language"],
        "model": job["model"],
        "diarization": diarization_model,
        "speakers": speaking,
        "source_file": job.get("source_name"),
        "source_url": job.get("source_url"),
        "job_id": job["id"],
        "generated_at": now_local(tz),
    }
    lines = [frontmatter(header), f"# {display_title(job)}", ""]
    for turn in turns:
        first, *rest = turn.paragraphs
        lines += [f"**{turn.name}** [{fmt_ts(first.start)}]", " ".join(first.texts), ""]
        for para in rest:
            lines += [f"[{fmt_ts(para.start)}] " + " ".join(para.texts), ""]
    return "\n".join(lines).rstrip() + "\n"


def slugify(text: str, max_len: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:max_len].rstrip("-") or "reunion"


def suggested_filename(job: dict, suffix: str = "") -> str:
    """Ex. 2026-10-01_14h30_point-hebdo.md (ou …_compte-rendu.md avec suffix)."""
    stamp = ""
    if job.get("meeting_date"):
        try:
            stamp = datetime.fromisoformat(job["meeting_date"]).strftime("%Y-%m-%d_%Hh%M") + "_"
        except ValueError:
            pass
    return f"{stamp}{slugify(display_title(job))}{suffix}.md"
