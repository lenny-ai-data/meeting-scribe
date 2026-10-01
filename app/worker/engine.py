"""Moteurs de transcription utilisés par le pipeline.

Format commun (celui de WhisperX) :
- segments : [{"start", "end", "text", "words": [{"word", "start", "end", "speaker"?}], "speaker"?}]
- diarisation : [{"start", "end", "speaker"}]
"""

import time
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from ..config import Settings

Progress = Callable[[float], None]  # pourcentage 0-100


class Engine(Protocol):
    def prepare_gpu(self, check_vram: bool, model: str) -> None: ...
    def load_audio(self, wav: Path) -> np.ndarray: ...
    def transcribe(self, audio: np.ndarray, model: str, language: str, vocabulary: str | None,
                   on_progress: Progress) -> dict: ...
    def align(self, result: dict, audio: np.ndarray, language: str, on_progress: Progress) -> dict: ...
    def diarize(self, audio: np.ndarray, num_speakers: int | None, min_speakers: int | None,
                max_speakers: int | None, on_progress: Progress) -> list[dict]: ...
    def assign_speakers(self, diarization: list[dict], aligned: dict) -> dict: ...


def make_engine(settings: Settings, device: str) -> Engine:
    if settings.fake_pipeline:
        return FakeEngine(settings.fake_pipeline_delay)
    from .whisperx_engine import WhisperXEngine

    return WhisperXEngine(settings, device)


class FakeEngine:
    """Moteur déterministe sans GPU : une phrase toutes les 4 s, deux intervenants en alternance."""

    SAMPLE_RATE = 16000
    CHUNK = 4.0

    def __init__(self, delay: float = 0.0):
        self.delay = delay  # secondes par segment, pour tester l'annulation

    def prepare_gpu(self, check_vram: bool, model: str) -> None:
        pass

    def load_audio(self, wav: Path) -> np.ndarray:
        import wave

        with wave.open(str(wav)) as w:
            frames = w.readframes(w.getnframes())
        return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0

    def _chunks(self, audio: np.ndarray) -> list[tuple[float, float]]:
        duration = len(audio) / self.SAMPLE_RATE
        out, t = [], 0.0
        while t + 1.0 < duration:
            out.append((t, min(t + self.CHUNK, duration)))
            t += self.CHUNK
        return out

    def transcribe(self, audio, model, language, vocabulary, on_progress):
        segments = []
        chunks = self._chunks(audio)
        for i, (start, end) in enumerate(chunks, 1):
            segments.append({"start": start, "end": end - 0.3, "text": f" Phrase numéro {i} de test."})
            time.sleep(self.delay)
            on_progress(100 * i / len(chunks))
        return {"segments": segments, "language": language}

    def align(self, result, audio, language, on_progress):
        aligned = []
        for seg in result["segments"]:
            words = seg["text"].split()
            step = (seg["end"] - seg["start"]) / len(words)
            aligned.append({
                **seg,
                "words": [{"word": w, "start": round(seg["start"] + k * step, 3),
                           "end": round(seg["start"] + (k + 1) * step, 3), "score": 0.9}
                          for k, w in enumerate(words)],
            })
        on_progress(100)
        return {"segments": aligned}

    def diarize(self, audio, num_speakers, min_speakers, max_speakers, on_progress):
        count = num_speakers or 2
        # Deux phrases par tour : SPEAKER_00, SPEAKER_00, SPEAKER_01, SPEAKER_01…
        out = [{"start": start, "end": end, "speaker": f"SPEAKER_{(i // 2) % count:02d}"}
               for i, (start, end) in enumerate(self._chunks(audio))]
        on_progress(100)
        return out

    def assign_speakers(self, diarization, aligned):
        def speaker_at(start: float, end: float) -> str | None:
            best, best_overlap = None, 0.0
            for d in diarization:
                overlap = min(end, d["end"]) - max(start, d["start"])
                if overlap > best_overlap:
                    best, best_overlap = d["speaker"], overlap
            return best

        for seg in aligned["segments"]:
            seg["speaker"] = speaker_at(seg["start"], seg["end"])
            for w in seg.get("words", []):
                w["speaker"] = speaker_at(w["start"], w["end"])
        return aligned
