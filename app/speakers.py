"""Post-traitement de la diarisation : libellés stables S1, S2… et temps de parole."""

import copy
from pathlib import Path

from . import db
from .jsonio import write_json


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


def speaker_intervals(diarization: list[dict]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for d in diarization:
        out.setdefault(d["speaker"], []).append((d["start"], d["end"]))
    return {sp: merge_intervals(iv) for sp, iv in out.items()}


def finalize(job: dict, job_dir: Path, diarization: list[dict], result: dict) -> None:
    """Relabellise, calcule les statistiques et enregistre intervenants et résultat."""
    mapping = build_mapping(diarization, result["segments"])
    diarization, segments = apply_mapping(diarization, result["segments"], mapping)
    speakers = [
        {"id": sp, "talk_time": round(sum(e - s for s, e in iv), 2), "first_start": iv[0][0]}
        for sp, iv in speaker_intervals(diarization).items()
    ]
    speakers.sort(key=lambda sp: int(sp["id"][1:]))
    write_json(job_dir / "diarization.json", diarization)
    write_json(job_dir / "result.json", {"segments": segments, "language": job["language"]})
    db.replace_speakers(job["id"], speakers)


def default_label(speaker_id: str | None, language: str) -> str:
    word = "Speaker" if language == "en" else "Intervenant"
    if not speaker_id or not speaker_id[1:].isdigit():
        return f"{word} ?"
    return f"{word} {speaker_id[1:]}"
