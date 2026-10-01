"""Fournisseur compatible OpenAI (OpenAI, Mistral, OpenRouter, Groq…) : POST {base_url}/chat/completions."""

import httpx

from .base import LLMError, LLMResult, strip_think


async def complete(base_url: str, api_key: str, model: str, system: str, user: str, *, timeout: float,
                   temperature: float | None = None) -> LLMResult:
    payload: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15)) as client:
        try:
            resp = await client.post(base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"API LLM injoignable ({base_url}) : {exc or type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise LLMError(f"L'API LLM a répondu {resp.status_code} : {resp.text[:300]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as exc:
        raise LLMError(f"Réponse inattendue de l'API LLM : {str(data)[:300]}") from exc
    usage = data.get("usage") or {}
    return LLMResult(
        content=strip_think(content),
        model=data.get("model") or model,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


async def list_models(base_url: str, api_key: str, timeout: float = 10) -> list[str]:
    """Modèles proposés par le service (GET {base_url}/models), quand il expose cette route."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(base_url.rstrip("/") + "/models", headers=headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"API LLM injoignable ({base_url}) : {exc or type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise LLMError(f"L'API LLM a répondu {resp.status_code} : {resp.text[:300]}")
    try:
        return sorted(m["id"] for m in resp.json()["data"])
    except (KeyError, TypeError, ValueError) as exc:
        raise LLMError(f"Liste de modèles inattendue : {resp.text[:300]}") from exc
