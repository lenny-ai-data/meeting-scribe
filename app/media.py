"""Outils audio autour de ffprobe / ffmpeg."""

import json
import subprocess
import wave
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .errors import ScribeError


class MediaError(ScribeError):
    pass


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise MediaError(f"{cmd[0]} a échoué : {tail}")
    return proc.stdout


def probe(path: Path) -> dict[str, Any]:
    """Durée (s), présence d'une piste audio et date d'enregistrement (creation_time) si connue."""
    out = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    data = json.loads(out)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    for stream in streams:
        for k, v in (stream.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    return {
        "duration": float(fmt["duration"]) if fmt.get("duration") else None,
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
        "creation_time": _parse_creation_time(tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")),
        "format": fmt.get("format_name"),
    }


def _parse_creation_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Horodatage nul posé par certains encodeurs
    if dt.year < 2000:
        return None
    return dt


def to_wav(src: Path, dst: Path) -> None:
    """Convertit n'importe quelle source (m4a, mp4, webm…) en WAV 16 kHz mono."""
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
          "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)])


def extract_clip(src: Path, start: float, end: float, dst: Path) -> None:
    """Extrait [start, end] en MP3 mono 64 kb/s (lisible partout, y compris Safari iOS)."""
    _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
          "-ss", f"{start:.3f}", "-t", f"{max(end - start, 0.1):.3f}", "-i", str(src),
          "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(dst)])


def waveform_peaks(wav: Path, bins: int) -> list[float]:
    """Enveloppe d'amplitude (RMS) d'un WAV 16 bits mono en `bins` valeurs entre 0 et 1 (frise de l'interface).

    Lecture par blocs : un enregistrement d'1 h 30 (170 Mo) n'est jamais chargé en entier.
    """
    with wave.open(str(wav)) as w:
        if w.getsampwidth() != 2:
            raise MediaError("WAV 16 bits attendu")
        per_bin = max(1, -(-w.getnframes() // bins))
        levels = []
        for _ in range(bins):
            chunk = np.frombuffer(w.readframes(per_bin), dtype="<i2")
            if not chunk.size:
                levels.append(0.0)
                continue
            samples = chunk.astype(np.float32) / 32768.0
            levels.append(float(np.sqrt(np.mean(samples ** 2))))
    top = max(levels, default=0.0)
    if top <= 0:
        return [0.0] * bins
    # Légère compression (racine) pour que les passages calmes restent visibles
    return [round((v / top) ** 0.6, 3) for v in levels]
