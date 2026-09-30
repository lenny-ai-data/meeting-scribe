import math
import re
from dataclasses import dataclass

from ..errors import ScribeError

CHARS_PER_TOKEN = 3.5      # estimation prudente pour du français
OUTPUT_RESERVE = 8192      # tokens réservés à la réponse
MIN_CTX = 8192


class LLMError(ScribeError):
    pass


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def compute_num_ctx(system: str, user: str, max_ctx: int) -> int:
    """Contexte à demander à Ollama : juste ce qu'il faut (le cache KV coûte de la VRAM)."""
    needed = int(estimate_tokens(system + user) * 1.15) + OUTPUT_RESERVE
    if needed > max_ctx:
        raise LLMError(
            f"Transcript trop long pour le contexte autorisé : environ {needed} tokens nécessaires, "
            f"OLLAMA_MAX_CTX={max_ctx}. Augmentez OLLAMA_MAX_CTX (jusqu'à 262144 pour Qwen3.8)."
        )
    return max(MIN_CTX, math.ceil(needed / 1024) * 1024)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think(text: str) -> str:
    return _THINK.sub("", text).strip()
