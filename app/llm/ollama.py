import logging

import httpx

from .base import LLMError, LLMResult, compute_num_ctx, strip_think

log = logging.getLogger("llm.ollama")


async def complete(base_url: str, model: str, system: str, user: str, *, think: bool, max_ctx: int,
                   timeout: float, temperature: float | None = None) -> LLMResult:
    num_ctx = compute_num_ctx(system, user, max_ctx)
    options: dict = {"num_ctx": num_ctx}
    if temperature is not None:
        options["temperature"] = temperature
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": think,
        "options": options,
    }
    log.info("Ollama %s, num_ctx=%d, think=%s", model, num_ctx, think)
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(timeout, connect=10)) as client:
        try:
            resp = await client.post("/api/chat", json=payload)
            if resp.status_code == 400 and "think" in resp.text:
                # Modèle sans mode « thinking »
                payload.pop("think")
                resp = await client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama injoignable ({base_url}) : {exc or type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise LLMError(f"Ollama a répondu {resp.status_code} : {resp.text[:300]}")
    data = resp.json()
    prompt_tokens = data.get("prompt_eval_count")
    if prompt_tokens and prompt_tokens >= num_ctx - 16:
        log.warning("Prompt probablement tronqué par Ollama (%d tokens pour num_ctx=%d)", prompt_tokens, num_ctx)
    return LLMResult(
        content=strip_think(data["message"]["content"]),
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=data.get("eval_count"),
    )
