"""Post-traitement de la diarisation : libellés stables S1, S2…, temps de parole et extraits audio."""

import copy
import logging
import shutil
from pathlib import Path

from . import db, media
from .jsonio import write_json

log = logging.getLogger("speakers")

SAMPLES_PER_SPEAKER = 3
MIN_CLIP = 2.5        # en dessous, l'extrait ne permet pas de reconnaître une voix
PREFERRED_CLIP = 4.0
MAX_CLIP = 12.0
MERGE_GAP = 0.5       # deux segments du même intervenant séparés de moins de 0,5 s n'en font qu'un
SAMPLE_TEXT_MAX = 220


def build_mapping(diarization: list[dict], segments: list[dict]) -> dict[str, str]:
    """SPEAKER_xx → S1, S2… dans l'ordre de première prise de parole."""
    order: list[str] = []
    for d in sorted(diarization, key=lambda d: d["start"]):
        if d["speaker"] not in order:
            order.append(d["speaker"])
    for seg in segments:
        sp = seg.get("speaker")
        if sp and sp not in order:
            order.append(sp)
    return {raw: f"S{i}" for i, raw in enumerate(order, 1)}


def apply_mapping(diarization: list[dict], segments: list[dict], mapping: dict[str, str]):
    diarization = [{**d, "speaker": mapping[d["speaker"]]} for d in diarization]
    segments = copy.deepcopy(segments)
    for seg in segments:
        if seg.get("speaker") in mapping:
            seg["speaker"] = mapping[seg["speaker"]]
        for w in seg.get("words", []):
            if w.get("speaker") in mapping:
                w["speaker"] = mapping[w["speaker"]]
    return diarization, segments


def merge_intervals(intervals: list[tuple[float, float]], gap: float = 0.0) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def speaker_intervals(diarization: list[dict], gap: float = 0.0) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for d in diarization:
        out.setdefault(d["speaker"], []).append((d["start"], d["end"]))
    return {sp: merge_intervals(iv, gap) for sp, iv in out.items()}


def subtract(intervals: list[tuple[float, float]], others: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Parties de `intervals` où personne d'autre ne parle."""
    out = []
    for start, end in intervals:
        pieces = [(start, end)]
        for o_start, o_end in others:
            if o_end <= start or o_start >= end:
                continue
            next_pieces = []
            for s, e in pieces:
                if o_end <= s or o_start >= e:
                    next_pieces.append((s, e))
                    continue
                if o_start > s:
                    next_pieces.append((s, o_start))
                if o_end < e:
                    next_pieces.append((o_end, e))
            pieces = next_pieces
        out.extend(pieces)
    return out


def pick_samples(clean: list[tuple[float, float]], fallback: list[tuple[float, float]], duration: float,
                 count: int = SAMPLES_PER_SPEAKER) -> list[tuple[float, float]]:
    """Choisit jusqu'à `count` extraits, de préférence sans voix superposée et répartis sur la réunion
    (un par tiers), chacun de MIN_CLIP à MAX_CLIP secondes."""
    candidates = [(s, min(e, s + MAX_CLIP)) for s, e in clean if e - s >= MIN_CLIP]
    if not candidates:
        # Intervenant qui ne parle jamais seul : on accepte la superposition
        candidates = [(s, min(e, s + MAX_CLIP)) for s, e in fallback if e - s >= 1.0]
    # Les plus longs d'abord (jusqu'à PREFERRED_CLIP, au-delà la longueur ne départage plus)
    candidates.sort(key=lambda c: (min(c[1] - c[0], PREFERRED_CLIP), c[1] - c[0]), reverse=True)

    chosen: list[tuple[float, float]] = []
    third = max(duration, 1e-6) / 3
    for k in range(3):
        for cand in candidates:
            if cand not in chosen and k * third <= (cand[0] + cand[1]) / 2 < (k + 1) * third:
                chosen.append(cand)
                break
    for cand in candidates:
        if len(chosen) >= count:
            break
        if cand not in chosen:
            chosen.append(cand)
    return sorted(chosen[:count])


def text_between(segments: list[dict], start: float, end: float) -> str:
    words = [
        w["word"].strip()
        for seg in segments
        if seg["end"] >= start and seg["start"] <= end
        for w in seg.get("words", [])
        if "start" in w and w["start"] >= start - 0.1 and w.get("end", w["start"]) <= end + 0.1
    ]
    if not words:
        words = [seg["text"].strip() for seg in segments if seg["end"] > start and seg["start"] < end]
    text = " ".join(w for w in words if w)
    return text if len(text) <= SAMPLE_TEXT_MAX else text[: SAMPLE_TEXT_MAX - 1].rstrip() + "…"


def analyze(diarization: list[dict], segments: list[dict], duration: float) -> list[dict]:
    """Statistiques et extraits par intervenant (diarisation déjà relabellisée en S1, S2…)."""
    by_speaker = speaker_intervals(diarization, MERGE_GAP)
    exact = speaker_intervals(diarization)
    speakers = []
    for sp, intervals in by_speaker.items():
        others = [iv for other, ivs in exact.items() if other != sp for iv in ivs]
        clips = pick_samples(subtract(intervals, others), intervals, duration)
        speakers.append({
            "id": sp,
            "talk_time": round(sum(e - s for s, e in exact[sp]), 2),
            "first_start": round(exact[sp][0][0], 2),
            "samples": [
                {"n": n, "start": round(s, 2), "end": round(e, 2), "text": text_between(segments, s, e)}
                for n, (s, e) in enumerate(clips, 1)
            ],
        })
    speakers.sort(key=lambda sp: int(sp["id"][1:]))
    return speakers


def sample_path(job_dir: Path, speaker_id: str, n: int) -> Path:
    return job_dir / "samples" / f"{speaker_id}_{n}.mp3"


def finalize(job: dict, job_dir: Path, diarization: list[dict], result: dict) -> None:
    """Relabellise, choisit et découpe les extraits, enregistre intervenants et résultat."""
    mapping = build_mapping(diarization, result["segments"])
    diarization, segments = apply_mapping(diarization, result["segments"], mapping)
    duration = job["duration"] or max((d["end"] for d in diarization), default=0.0)
    speakers = analyze(diarization, segments, duration)

    samples_dir = job_dir / "samples"
    shutil.rmtree(samples_dir, ignore_errors=True)
    samples_dir.mkdir()
    for sp in speakers:
        for sample in sp["samples"]:
            media.extract_clip(job_dir / "audio.wav", sample["start"], sample["end"],
                               sample_path(job_dir, sp["id"], sample["n"]))

    write_json(job_dir / "diarization.json", diarization)
    write_json(job_dir / "result.json", {"segments": segments, "language": job["language"]})
    # Les libellés changent à chaque diarisation : les noms donnés précédemment ne s'appliquent plus
    db.replace_speakers(job["id"], speakers)
    log.info("%d intervenant(s) : %s", len(speakers), ", ".join(f"{s['id']}={s['talk_time']:.0f}s" for s in speakers))


def default_label(speaker_id: str | None, language: str) -> str:
    word = "Speaker" if language == "en" else "Intervenant"
    if not speaker_id or not speaker_id[1:].isdigit():
        return f"{word} ?"
    return f"{word} {speaker_id[1:]}"
