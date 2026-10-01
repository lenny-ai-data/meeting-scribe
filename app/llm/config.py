"""Configuration effective des fournisseurs LLM.

Les réglages enregistrés depuis l'interface ou l'API (table `settings`, clé `llm`) priment
sur le .env, champ par champ : un champ absent de la base reprend la valeur du .env.
"""

from dataclasses import dataclass
from urllib.parse import urlparse

from .. import db
from ..config import get_settings

SETTING_KEY = "llm"
# Ollama sur la même machine que Meeting Scribe : il partage le GPU, il faut le décharger
LOCAL_HOSTS = {"host.docker.internal", "localhost", "127.0.0.1", "::1"}


@dataclass
class OllamaConfig:
    url: str
    model: str
    max_ctx: int
    unload_before_gpu: bool | None  # None = automatique

    @property
    def unload(self) -> bool:
        """Décharger Ollama avant un job GPU : par défaut, seulement s'il tourne sur cette machine."""
        if self.unload_before_gpu is not None:
            return self.unload_before_gpu
        return (urlparse(self.url).hostname or "") in LOCAL_HOSTS


@dataclass
class OpenAIConfig:
    base_url: str
    api_key: str
    model: str

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class LLMConfig:
    ollama: OllamaConfig
    openai: OpenAIConfig


def stored() -> dict:
    return db.get_setting(SETTING_KEY) or {"ollama": {}, "openai": {}}


def llm_config() -> LLMConfig:
    settings = get_settings()
    saved = stored()
    o, a = saved.get("ollama", {}), saved.get("openai", {})
    return LLMConfig(
        ollama=OllamaConfig(
            url=o.get("url", settings.ollama_url),
            model=o.get("model", settings.ollama_model),
            max_ctx=o.get("max_ctx", settings.ollama_max_ctx),
            unload_before_gpu=o.get("unload_before_gpu", settings.ollama_unload_before_gpu),
        ),
        openai=OpenAIConfig(
            base_url=a.get("base_url", settings.llm_api_base_url),
            api_key=a.get("api_key", settings.llm_api_key),
            model=a.get("model", settings.llm_api_model),
        ),
    )
