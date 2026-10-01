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


CLOSING_PUNCT = re.compile(r"^[?!:;.,…»)\]]+$")
SENTENCE_END = (".", "?", "!", "…")
# Recalage des changements d'intervenant : on déplace au plus SNAP_MAX_WORDS mots représentant au plus
# SNAP_MAX_SPAN secondes de parole (au-delà, c'est plus probablement une vraie interruption), et jamais
# à travers une pause de plus de SNAP_MAX_GAP secondes (la frontière détectée est alors plausible).
SNAP_MAX_WORDS = 4
SNAP_MAX_SPAN = 2.0
SNAP_MAX_GAP = 1.0


def _word_stream(segments: list[dict]) -> list[dict]:
    """Tous les mots du transcript, avec horodatage, intervenant et indice de segment toujours renseignés."""
    out: list[dict] = []
    for idx, seg in enumerate(segments):
        words = [w for w in seg.get("words", []) if w.get("word", "").strip()]
        if not any("start" in w for w in words):
            # Segment non aligné : traité comme un seul « mot »
            if seg.get("text", "").strip():
                out.append({"word": seg["text"].strip(), "start": seg["start"], "end": seg["end"],
                            "speaker": seg.get("speaker"), "seg": idx})
            continue
        speaker, t = seg.get("speaker"), seg["start"]
        for w in words:
            text = w["word"].strip()
            # Ponctuation isolée (« vous l'entendez ? ») : l'alignement lui donne l'horodatage du mot
            # suivant, parfois prononcé par quelqu'un d'autre ; elle appartient au mot précédent.
            if CLOSING_PUNCT.match(text) and out and out[-1]["seg"] == idx:
                out[-1]["word"] += " " + text
                continue
            # Mots sans horodatage (nombres, sigles) ou sans intervenant : hérités du mot précédent
            speaker = w.get("speaker") or speaker
            start = w.get("start", t)
            t = w.get("end", start)
            out.append({"word": text, "start": start, "end": t, "speaker": speaker, "seg": idx})
    return out


def _runs(words: list[dict]) -> list[list[dict]]:
    runs: list[list[dict]] = []
    for w in words:
        if runs and runs[-1][0]["speaker"] == w["speaker"]:
            runs[-1].append(w)
        else:
            runs.append([w])
    return runs


def _relabel(words: list[dict], speaker: str | None) -> list[dict]:
    for w in words:
        w["speaker"] = speaker
    return words


def _smooth(runs: list[list[dict]]) -> list[list[dict]]:
    """Rattache un mot isolé attribué à un autre intervenant au milieu d'une même voix
    (bruit de diarisation), mais garde les vraies interjections de plusieurs mots."""
    out: list[list[dict]] = []
    i = 0
    while i < len(runs):
        run = runs[i]
        if (out and i + 1 < len(runs) and len(run) == 1
                and out[-1][0]["speaker"] == runs[i + 1][0]["speaker"] != run[0]["speaker"]):
            out[-1].extend(_relabel(run + runs[i + 1], out[-1][0]["speaker"]))
            i += 2
            continue
        if out and out[-1][0]["speaker"] == run[0]["speaker"]:
            out[-1].extend(run)
        else:
            out.append(list(run))
        i += 1
    return out


def _ends_sentence(word: str) -> bool:
    return word.rstrip("»)\"' ").endswith(SENTENCE_END)


def _snap(runs: list[list[dict]]) -> list[list[dict]]:
    """Recale chaque changement d'intervenant sur la fin de phrase la plus proche.

    Les frontières de diarisation arrivent souvent avec un léger décalage : quelques mots de début
    de phrase restent au précédent intervenant (« … Mais Julien | Audoul, certains… »), ou la fin
    d'une phrase part chez le suivant (« … devront | répondre. Justement… »).
    """
    for i in range(len(runs) - 1):
        a, b = runs[i], runs[i + 1]
        if not a or not b or _ends_sentence(a[-1]["word"]):
            continue
        # Option 1 : la fin de `a` après sa dernière phrase complète passe chez `b`
        last_end = max((k for k, w in enumerate(a) if _ends_sentence(w["word"])), default=None)
        tail = len(a) - last_end - 1 if last_end is not None else None
        gap_ok = b[0]["start"] - a[-1]["end"] <= SNAP_MAX_GAP
        tail_ok = (gap_ok and tail is not None and 0 < tail <= SNAP_MAX_WORDS
                   and a[-1]["end"] - a[-tail]["start"] <= SNAP_MAX_SPAN)
        # Option 2 : le début de `b` jusqu'à sa première fin de phrase revient à `a`
        first_end = next((k for k, w in enumerate(b) if _ends_sentence(w["word"])), None)
        head = first_end + 1 if first_end is not None else None
        head_ok = (gap_ok and head is not None and head <= SNAP_MAX_WORDS and head < len(b)
                   and b[head - 1]["end"] - b[0]["start"] <= SNAP_MAX_SPAN)
        if tail_ok and (not head_ok or tail <= head):
            runs[i + 1] = _relabel(a[-tail:], b[0]["speaker"]) + b
            runs[i] = a[:-tail]
        elif head_ok:
            runs[i] = a + _relabel(b[:head], a[0]["speaker"])
            runs[i + 1] = b[head:]
    return [run for run in runs if run]


def build_units(segments: list[dict]) -> list[Unit]:
    """Découpe le transcript aux changements d'intervenant détectés au niveau des mots
    (et aux limites de segments Whisper, qui servent au découpage en paragraphes)."""
    runs = _runs(_word_stream(segments))
    runs = _snap(_smooth(runs))
    runs = _smooth(runs)  # le recalage peut rendre adjacents deux passages du même intervenant
    units = []
    for run in runs:
        piece: list[dict] = []
        for w in run:
            if piece and w["seg"] != piece[-1]["seg"]:
                units.append(Unit(piece[0]["speaker"], piece[0]["start"], piece[-1]["end"],
                                  " ".join(x["word"] for x in piece)))
                piece = []
            piece.append(w)
        units.append(Unit(piece[0]["speaker"], piece[0]["start"], piece[-1]["end"], " ".join(x["word"] for x in piece)))
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
        "profile": job.get("profile"),
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
