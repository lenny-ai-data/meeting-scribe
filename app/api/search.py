from fastapi import APIRouter, Query

from .. import search as engine

router = APIRouter(tags=["search"])


@router.get("/search", summary="Rechercher dans les transcripts des réunions terminées")
def search(
    q: str = Query(..., min_length=2, max_length=200,
                   description="Termes cherchés, tous présents dans un même paragraphe ; insensible à la casse "
                               "et aux accents. \"Entre guillemets\" : expression exacte."),
    per_job: int = Query(3, ge=0, le=50, description="Passages renvoyés par réunion (tous sont comptés)"),
    limit: int = Query(50, ge=1, le=200, description="Nombre maximal de réunions"),
):
    """Parcourt les transcripts terminés, de la réunion la plus récente à la plus ancienne. Chaque passage
    porte son horodatage (`start`, en secondes), l'intervenant (nom courant), un extrait et les positions
    `[début, fin[` des termes trouvés dans cet extrait (`highlights`)."""
    return engine.search(q, per_job, limit)
