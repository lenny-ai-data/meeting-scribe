"""Profils de performance : un modèle Whisper et un pas de diarisation, selon le matériel.

Mesures de référence (01/10/2026), interview de 7 min 54 en français, modèles en cache :
i7-12700 (10 threads) pour le CPU, RTX 3090 pour le GPU. Détail dans AGENTS.md.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    model: str
    diarization_step: float  # secondes entre deux fenêtres de 10 s de pyannote (1 s par défaut)
    description: str


PROFILE_IDS = ("tres_rapide", "rapide", "precis", "tres_precis")

PROFILES: dict[str, dict[str, Profile]] = {
    "cpu": {p.id: p for p in (
        Profile("tres_rapide", "Très rapide", "small", 5.0,
                "small, diarisation au pas de 5 s : environ 0,25 × la durée de la réunion. "
                "Indiquer le nombre d’intervenants, sans quoi deux voix risquent d’être confondues."),
        Profile("rapide", "Rapide", "small", 2.5,
                "small, diarisation au pas de 2,5 s : environ 0,3 × la durée de la réunion. "
                "Transcription moins fidèle (noms propres, chiffres), que le compte rendu rattrape."),
        Profile("precis", "Précis", "large-v3-turbo", 2.5,
                "large-v3-turbo, diarisation au pas de 2,5 s : environ 0,4 × la durée de la réunion."),
        Profile("tres_precis", "Très précis", "large-v3-turbo", 1.0,
                "large-v3-turbo, diarisation au pas de 1 s : environ 0,6 × la durée de la réunion. "
                "Les interventions brèves sont mieux attribuées."),
    )},
    "cuda": {p.id: p for p in (
        Profile("tres_rapide", "Très rapide", "small", 2.5,
                "small, diarisation au pas de 2,5 s : pour les cartes de 3 à 4 Go. "
                "Transcription moins fidèle (noms propres, chiffres)."),
        Profile("rapide", "Rapide", "large-v3-turbo", 2.5,
                "large-v3-turbo, diarisation au pas de 2,5 s."),
        Profile("precis", "Précis", "large-v3", 2.5,
                "large-v3, diarisation au pas de 2,5 s."),
        Profile("tres_precis", "Très précis", "large-v3", 1.0,
                "large-v3, diarisation au pas de 1 s : la meilleure qualité, "
                "quelques secondes de plus que « Précis »."),
    )},
}

DEFAULT_PROFILE = {"cpu": "rapide", "cuda": "tres_precis"}


def get_profile(device: str, profile_id: str) -> Profile:
    return PROFILES[device][profile_id]


# --- VRAM -----------------------------------------------------------------------------

# Pic de VRAM de la transcription (Go), chargement du modèle compris, par (précision, taille des lots).
# La transcription est l'étape la plus gourmande ; l'alignement prend 0,7 Go, la diarisation 1,6 Go.
TRANSCRIBE_VRAM_GB = {
    "small": {("float16", 16): 2.5, ("int8_float16", 16): 2.1, ("int8_float16", 8): 1.5, ("int8_float16", 4): 1.2},
    "large-v3-turbo": {("float16", 16): 4.3, ("int8_float16", 16): 3.3, ("int8_float16", 8): 2.6,
                       ("int8_float16", 4): 2.0},
    "large-v3": {("float16", 16): 9.8, ("int8_float16", 16): 8.0, ("int8_float16", 8): 5.4, ("int8_float16", 4): 3.9},
}
# Configurations essayées dans l'ordre, de la plus rapide à la plus économe (au plus 4 s d'écart sur
# 7 min 54) ; int8 ne change pas la qualité (WER de turbo : 6,2 % en int8, 6,4 % en float16)
GPU_CONFIGS = (("float16", 16), ("int8_float16", 16), ("int8_float16", 8), ("int8_float16", 4))
DIARIZATION_VRAM_GB = 1.6
# Contexte CUDA, fragmentation, affichage éventuel sur la même carte
VRAM_MARGIN_GB = 1.0


def min_vram_gb(model: str | None) -> float:
    """VRAM libre minimale pour un job : configuration la plus économe du modèle, ou diarisation seule."""
    need = DIARIZATION_VRAM_GB
    if model in TRANSCRIBE_VRAM_GB:
        need = max(need, min(TRANSCRIBE_VRAM_GB[model].values()))
    return round(need + VRAM_MARGIN_GB, 1)


def choose_gpu_config(model: str, free_gb: float, batch_override: int | None = None) -> tuple[str, int]:
    """Précision et taille des lots les plus rapides qui tiennent dans la VRAM libre."""
    peaks = TRANSCRIBE_VRAM_GB.get(model, TRANSCRIBE_VRAM_GB["large-v3"])
    if batch_override:
        fits_fp16 = peaks[("float16", 16)] + VRAM_MARGIN_GB <= free_gb
        return ("float16" if fits_fp16 else "int8_float16"), batch_override
    for config in GPU_CONFIGS:
        if peaks[config] + VRAM_MARGIN_GB <= free_gb:
            return config
    return GPU_CONFIGS[-1]
