"""Moteur réel : WhisperX (faster-whisper + alignement wav2vec2 + pyannote).

Les modèles sont chargés l'un après l'autre et libérés entre chaque étape.
"""

import gc
import inspect
import logging
import os
from pathlib import Path

from ..config import Settings
from ..errors import ScribeError
from . import gpu

log = logging.getLogger("whisperx")


class WhisperXEngine:
    def __init__(self, settings: Settings, device: str):
        self.settings = settings
        self.device = device
        self.compute_type = "float16" if device == "cuda" else "int8"

    def _free(self) -> None:
        gc.collect()
        if self.device == "cuda":
            import torch

            torch.cuda.empty_cache()

    def prepare_gpu(self, check_vram: bool) -> None:
        if self.device != "cuda":
            return
        unloaded = gpu.unload_ollama(self.settings.ollama_url, self.settings.ollama_unload_timeout)
        if unloaded:
            log.info("Modèles Ollama déchargés : %s", ", ".join(unloaded))
        if check_vram:
            gpu.check_free_vram(self.settings.min_free_vram_gb)

    def load_audio(self, wav: Path):
        import whisperx

        return whisperx.load_audio(str(wav))

    def transcribe(self, audio, model, language, vocabulary, on_progress):
        import whisperx

        asr_options = {"initial_prompt": vocabulary} if vocabulary else None
        log.info("Chargement de %s (%s, %s)", model, self.device, self.compute_type)
        pipeline = whisperx.load_model(
            model, self.device, compute_type=self.compute_type, language=language,
            asr_options=asr_options, threads=max(4, (os.cpu_count() or 8) // 2),
        )
        kwargs = {"batch_size": self.settings.batch_size, "language": language, "progress_callback": on_progress}
        # Option présente seulement dans les versions récentes de WhisperX
        if "interleaved_context" in inspect.signature(pipeline.transcribe).parameters:
            kwargs["interleaved_context"] = True
        result = pipeline.transcribe(audio, **kwargs)
        del pipeline
        self._free()
        return result

    def align(self, result, audio, language, on_progress):
        import whisperx

        model, metadata = whisperx.load_align_model(language_code=language, device=self.device)
        aligned = whisperx.align(result["segments"], model, metadata, audio, self.device,
                                 return_char_alignments=False, progress_callback=on_progress)
        del model
        self._free()
        return {"segments": aligned["segments"]}

    def diarize(self, audio, num_speakers, min_speakers, max_speakers, on_progress):
        if not self.settings.hf_token:
            raise ScribeError("HF_TOKEN manquant : la diarisation pyannote exige un jeton Hugging Face (voir README).")
        from whisperx.diarize import DiarizationPipeline

        try:
            pipeline = DiarizationPipeline(model_name=self.settings.diarization_model,
                                           token=self.settings.hf_token, device=self.device)
        except Exception as exc:
            if "403" in str(exc) or "401" in str(exc) or "gated" in str(exc).lower():
                raise ScribeError(
                    f"Accès refusé à {self.settings.diarization_model} : acceptez ses conditions sur "
                    f"huggingface.co avec le compte du jeton HF_TOKEN. ({exc})"
                ) from exc
            raise
        df = pipeline(audio, num_speakers=num_speakers, min_speakers=min_speakers, max_speakers=max_speakers,
                      progress_callback=on_progress)
        del pipeline
        self._free()
        return [{"start": float(r.start), "end": float(r.end), "speaker": str(r.speaker)}
                for r in df.itertuples()]

    def assign_speakers(self, diarization, aligned):
        import pandas as pd
        import whisperx

        df = pd.DataFrame(diarization, columns=["start", "end", "speaker"])
        return whisperx.assign_word_speakers(df, aligned, fill_nearest=True)
