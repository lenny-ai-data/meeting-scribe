"""Réglages des fournisseurs LLM, modifiables depuis l'interface ou par l'API.

Les valeurs enregistrées priment sur le .env ; `null` rend la valeur du .env.
La clé d'API est en écriture seule : elle n'est jamais renvoyée.
"""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import db
from ..llm import ollama, openai_compat
from ..llm.base import LLMError
from ..llm.config import SETTING_KEY, llm_config, stored

router = APIRouter(tags=["settings"])


def _check_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise ValueError("l'adresse doit commencer par http:// ou https://")
    return value


class OllamaUpdate(BaseModel):
    url: str | None = Field(None, description="Ex. http://host.docker.internal:11434, ou http://192.168.1.20:11434")
    model: str | None = Field(None, description="Modèle par défaut des comptes rendus")
    max_ctx: int | None = Field(None, ge=2048, le=1_048_576, description="Contexte maximal demandé (tokens)")
    unload_before_gpu: bool | None = Field(
        None, description="Décharger Ollama avant un job GPU ; null = automatique (seulement s'il tourne sur cette machine)")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


class OpenAIUpdate(BaseModel):
    base_url: str | None = Field(None, description="Ex. https://api.mistral.ai/v1, ou http://host.docker.internal:1234/v1")
    api_key: str | None = Field(None, description="Écriture seule ; chaîne vide = pas de clé")
    model: str | None = None

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


class LLMSettingsUpdate(BaseModel):
    """Mise à jour partielle : un champ omis est conservé, un champ à `null` reprend la valeur du .env."""

    ollama: OllamaUpdate | None = None
    openai: OpenAIUpdate | None = None


class LLMTestRequest(BaseModel):
    provider: Literal["ollama", "openai"]
    url: str | None = Field(None, description="Adresse à tester à la place de celle enregistrée")
    api_key: str | None = Field(None, description="Clé à tester à la place de celle enregistrée (API OpenAI)")

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


def llm_settings_out() -> dict:
    cfg, saved = llm_config(), stored()
    return {
        "ollama": {"url": cfg.ollama.url, "model": cfg.ollama.model or None, "max_ctx": cfg.ollama.max_ctx,
                   "unload_before_gpu": cfg.ollama.unload_before_gpu, "unload_effective": cfg.ollama.unload},
        "openai": {"base_url": cfg.openai.base_url or None, "model": cfg.openai.model or None,
                   "api_key_set": bool(cfg.openai.api_key), "configured": cfg.openai.configured},
        # Champs enregistrés en base (les autres viennent du .env)
        "overrides": {name: sorted(saved.get(name, {})) for name in ("ollama", "openai")},
    }


@router.get("/settings/llm", summary="Réglages des fournisseurs LLM (la clé d'API n'est jamais renvoyée)")
def get_llm_settings():
    return llm_settings_out()


@router.put("/settings/llm", summary="Modifier les réglages LLM (mise à jour partielle ; null = valeur du .env)")
def update_llm_settings(body: LLMSettingsUpdate):
    saved = stored()
    for name in ("ollama", "openai"):
        section = getattr(body, name)
        if section is None:
            continue
        current = saved.setdefault(name, {})
        for field in section.model_fields_set:
            value = getattr(section, field)
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
    db.set_setting(SETTING_KEY, saved)
    return llm_settings_out()


async def _models(provider: str, url: str | None = None, api_key: str | None = None) -> list[str]:
    cfg = llm_config()
    if provider == "ollama":
        return await ollama.list_models(url or cfg.ollama.url)
    base_url = url or cfg.openai.base_url
    if not base_url:
        raise LLMError("Adresse de l'API non renseignée")
    return await openai_compat.list_models(base_url, cfg.openai.api_key if api_key is None else api_key)


@router.post("/settings/llm/test", summary="Tester un fournisseur LLM : liste ses modèles ou renvoie l'erreur")
async def test_llm(body: LLMTestRequest):
    try:
        models = await _models(body.provider, body.url, body.api_key)
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "models": []}
    return {"ok": True, "error": None, "models": models}


@router.get("/llm/models", summary="Modèles disponibles chez un fournisseur LLM")
async def list_llm_models(provider: Literal["ollama", "openai"] = "ollama"):
    try:
        return {"provider": provider, "models": await _models(provider)}
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc
