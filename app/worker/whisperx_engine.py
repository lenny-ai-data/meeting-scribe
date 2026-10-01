"""Moteur réel : WhisperX (faster-whisper + alignement wav2vec2 + pyannote).

Les modèles sont chargés l'un après l'autre et libérés entre chaque étape.
"""

import fnmatch
import gc
import inspect
import logging
import os
import threading
from pathlib import Path

from ..config import Settings
from ..errors import ScribeError
from ..llm.config import llm_config
from . import gpu
from .engine import Detail, no_detail

log = logging.getLogger("whisperx")


# Fichiers téléchargés par faster-whisper (faster_whisper.utils.download_model)
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def format_size(n: float) -> str:
    """Taille lisible, à la française : 850 Mo, 1,6 Go."""
    if n < 1e9:
        return f"{n / 1e6:.0f} Mo"
    return f"{n / 1e9:.1f} Go".replace(".", ",")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.iterdir() if f.is_file())


class WhisperXEngine:
    def __init__(self, settings: Settings, device: str, on_detail: Detail = no_detail):
        self.settings = settings
        self.device = device
        self.compute_type = "float16" if device == "cuda" else "int8"
        self.on_detail = on_detail

    def _free(self, step: str) -> None:
        gc.collect()
        if self.device == "cuda":
            import torch

            # Pic réellement alloué par torch : sert à régler MIN_VRAM_GB (hors mémoire de ctranslate2)
            log.info("%s : pic de VRAM allouée par torch %.1f Go", step, torch.cuda.max_memory_allocated() / 2**30)
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.empty_cache()

    def prepare_gpu(self, check_vram: bool, model: str) -> None:
        if self.device != "cuda":
            return
        ollama = llm_config().ollama
        if ollama.unload:
            unloaded = gpu.unload_ollama(ollama.url, self.settings.ollama_unload_timeout)
            if unloaded:
                log.info("Modèles Ollama déchargés : %s", ", ".join(unloaded))
        else:
            log.info("Ollama (%s) ne partage pas ce GPU : pas de déchargement", ollama.url)
        if check_vram:
            gpu.check_free_vram(self.settings.min_vram_gb(model))

    def load_audio(self, wav: Path):
        import whisperx

        return whisperx.load_audio(str(wav))

    def _repo_size(self, repo: str) -> int | None:
        """Taille totale des fichiers du modèle sur Hugging Face, ou None si elle est inconnue (hors ligne…)."""
        try:
            from huggingface_hub import HfApi

            info = HfApi().model_info(repo, files_metadata=True)
        except Exception as exc:
            log.info("Taille de %s inconnue : %s", repo, exc)
            return None
        sizes = [f.size or 0 for f in info.siblings or []
                 if any(fnmatch.fnmatch(f.rfilename, pattern) for pattern in WHISPER_FILES)]
        return sum(sizes) or None

    def _download_whisper(self, model: str) -> None:
        """Télécharge le modèle au premier usage en suivant le cache : sans cela, l'étape reste
        à 0 % pendant de longues minutes (1,6 Go pour large-v3-turbo, 3 Go pour large-v3)."""
        from faster_whisper.utils import _MODELS, download_model
        from huggingface_hub.constants import HF_HUB_CACHE

        try:
            download_model(model, local_files_only=True)
            return
        except Exception:
            pass
        repo = _MODELS.get(model, model)
        total = self._repo_size(repo)
        blobs = Path(HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}" / "blobs"
        errors: list[BaseException] = []

        def run() -> None:
            try:
                download_model(model)
            except BaseException as exc:
                errors.append(exc)

        log.info("Téléchargement de %s (%s)", repo, format_size(total) if total else "taille inconnue")
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while thread.is_alive():
            thread.join(1.0)
            done = _dir_size(blobs)
            text = f"Téléchargement du modèle {model} (premier usage) : {format_size(done)}"
            if total:
                self.on_detail(f"{text} sur {format_size(total)}", min(99.0, 100 * done / total))
            else:
                self.on_detail(text, None)
        if errors:
            raise ScribeError(f"Téléchargement du modèle {model} impossible : {errors[0]}") from errors[0]

    def transcribe(self, audio, model, language, vocabulary, on_progress):
        import whisperx

        self._download_whisper(model)
        asr_options = {"initial_prompt": vocabulary} if vocabulary else None
        log.info("Chargement de %s (%s, %s)", model, self.device, self.compute_type)
        self.on_detail(f"Chargement du modèle {model}", 0)
        pipeline = whisperx.load_model(
            model, self.device, compute_type=self.compute_type, language=language,
            asr_options=asr_options, threads=max(4, (os.cpu_count() or 8) // 2),
        )
        batch_size = self.settings.batch_size_for(self.device)
        self.on_detail(f"Transcription par lots de {batch_size} passages de 30 s", 0)
        kwargs = {"batch_size": batch_size, "language": language, "progress_callback": on_progress}
        # Option présente seulement dans les versions récentes de WhisperX
        if "interleaved_context" in inspect.signature(pipeline.transcribe).parameters:
            kwargs["interleaved_context"] = True
        result = pipeline.transcribe(audio, **kwargs)
        del pipeline
        self._free("Transcription")
        return result

    def align(self, result, audio, language, on_progress):
        import whisperx

        self.on_detail("Chargement du modèle d’alignement (téléchargé au premier usage)", None)
        model, metadata = whisperx.load_align_model(language_code=language, device=self.device)
        self.on_detail(None, None)
        aligned = whisperx.align(result["segments"], model, metadata, audio, self.device,
                                 return_char_alignments=False, progress_callback=on_progress)
        del model
        self._free("Alignement")
        return {"segments": aligned["segments"]}

    def diarize(self, audio, num_speakers, min_speakers, max_speakers, on_progress):
        settings = self.settings
        if not settings.diarization_ready:
            raise ScribeError(
                f"HF_TOKEN manquant : le modèle {settings.diarization_model} n'est pas embarqué dans l'image "
                "et son téléchargement exige un jeton Hugging Face (voir README)."
            )
        from whisperx.diarize import DiarizationPipeline

        source = settings.diarization_source
        log.info("Modèle de diarisation : %s", source)
        try:
            pipeline = DiarizationPipeline(model_name=source, token=settings.hf_token or None, device=self.device)
        except Exception as exc:
            if "403" in str(exc) or "401" in str(exc) or "gated" in str(exc).lower():
                raise ScribeError(
                    f"Accès refusé à {self.settings.diarization_model} : acceptez ses conditions sur "
                    f"huggingface.co avec le compte du jeton HF_TOKEN. ({exc})"
                ) from exc
            raise
        if self.device == "cpu":
            segmentation = pipeline.model._segmentation
            segmentation.step = min(settings.cpu_diarization_step, segmentation.duration)
            log.info("Diarisation sur CPU : fenêtres de %g s, pas de %g s", segmentation.duration, segmentation.step)
        df = pipeline(audio, num_speakers=num_speakers, min_speakers=min_speakers, max_speakers=max_speakers,
                      progress_callback=on_progress)
        del pipeline
        self._free("Diarisation")
        return [{"start": float(r.start), "end": float(r.end), "speaker": str(r.speaker)}
                for r in df.itertuples()]

    def assign_speakers(self, diarization, aligned):
        import pandas as pd
        import whisperx

        df = pd.DataFrame(diarization, columns=["start", "end", "speaker"])
        return whisperx.assign_word_speakers(df, aligned, fill_nearest=True)
