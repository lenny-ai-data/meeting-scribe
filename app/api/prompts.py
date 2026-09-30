from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from .. import db

router = APIRouter(tags=["prompts"])


class PromptIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    is_default: bool = False


def prompt_or_404(prompt_id: str) -> dict:
    prompt = db.get_prompt(prompt_id)
    if prompt is None:
        raise HTTPException(404, "Prompt introuvable")
    return prompt


@router.get("/prompts", summary="Prompts système (le prompt par défaut en premier)")
def list_prompts():
    return db.list_prompts()


@router.post("/prompts", status_code=201, summary="Créer un prompt système")
def create_prompt(body: PromptIn):
    return db.save_prompt(None, body.name.strip(), body.content, body.is_default)


@router.get("/prompts/{prompt_id}", summary="Lire un prompt système")
def get_prompt(prompt_id: str):
    return prompt_or_404(prompt_id)


@router.put("/prompts/{prompt_id}", summary="Modifier un prompt système")
def update_prompt(prompt_id: str, body: PromptIn):
    prompt_or_404(prompt_id)
    return db.save_prompt(prompt_id, body.name.strip(), body.content, body.is_default)


@router.delete("/prompts/{prompt_id}", status_code=204, summary="Supprimer un prompt système")
def delete_prompt(prompt_id: str):
    prompt_or_404(prompt_id)
    if db.count_prompts() <= 1:
        raise HTTPException(409, "Impossible de supprimer le dernier prompt")
    db.delete_prompt(prompt_id)
    return Response(status_code=204)
