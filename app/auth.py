import secrets

from fastapi import HTTPException, Request

from .config import get_settings


async def require_token(request: Request) -> None:
    """Jeton optionnel (API_TOKEN). En-tête `Authorization: Bearer …`, ou `?token=…`
    pour les éléments <audio> et les liens de téléchargement qui ne peuvent pas porter d'en-tête."""
    expected = get_settings().api_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
    if not secrets.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Jeton d'API manquant ou invalide",
                            headers={"WWW-Authenticate": "Bearer"})
