"""Libération de la carte graphique avant de charger Whisper / pyannote.

La RTX 3090 (24 Go) ne peut pas porter à la fois le LLM d'Ollama (~17 Go) et la chaîne WhisperX.
"""

import logging
import time

import httpx

from ..errors import ScribeError

log = logging.getLogger("gpu")

GiB = 1024**3


class GpuBusyError(ScribeError):
    pass


def unload_ollama(base_url: str, timeout: float) -> list[str]:
    """Décharge tous les modèles chargés par Ollama et attend que la mémoire soit rendue."""
    with httpx.Client(base_url=base_url, timeout=15) as client:
        try:
            loaded = [m["name"] for m in client.get("/api/ps").json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Ollama injoignable (%s) : rien à décharger", exc)
            return []
        if not loaded:
            return []
        log.info("Déchargement des modèles Ollama : %s", ", ".join(loaded))
        for name in loaded:
            resp = client.post("/api/generate", json={"model": name, "keep_alive": 0})
            if resp.status_code >= 400:
                # Modèle d'embedding : pas de /api/generate
                client.post("/api/embed", json={"model": name, "input": "", "keep_alive": 0})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not client.get("/api/ps").json().get("models"):
                return loaded
            time.sleep(1)
    raise GpuBusyError(f"Ollama n'a pas libéré ses modèles ({', '.join(loaded)}) en {timeout:.0f} s.")


def free_vram_gb() -> float:
    import torch

    return torch.cuda.mem_get_info()[0] / GiB


def check_free_vram(min_free_gb: float) -> float:
    import torch

    if not torch.cuda.is_available():
        raise GpuBusyError("Aucun GPU CUDA visible dans le conteneur (utilisez device=cpu, ou vérifiez le runtime NVIDIA).")
    free, total = torch.cuda.mem_get_info()
    log.info("VRAM libre : %.1f / %.1f Go", free / GiB, total / GiB)
    if total / GiB < min_free_gb:
        raise GpuBusyError(
            f"Carte de {total / GiB:.1f} Go : trop petite pour ce profil ({min_free_gb:g} Go de VRAM libre requis). "
            "Choisissez un profil plus rapide, ou le CPU."
        )
    if free / GiB < min_free_gb:
        raise GpuBusyError(
            f"VRAM libre insuffisante : {free / GiB:.1f} Go sur {total / GiB:.1f} Go (minimum {min_free_gb:g} Go). "
            "Un autre programme occupe la carte (un modèle rechargé par un autre client d'Ollama, un autre service "
            "de transcription…). Réessayez plus tard, ou choisissez un profil plus rapide."
        )
    return free / GiB
