from fastapi import APIRouter

from .. import db
from ..speakers import default_label
from .common import job_or_404

router = APIRouter(tags=["speakers"])


def speakers_out(job: dict) -> list[dict]:
    speakers = db.get_speakers(job["id"])
    total = sum(sp["talk_time"] for sp in speakers) or 1.0
    return [
        {
            "id": sp["id"],
            "name": sp["name"],
            "display_name": sp["name"] or default_label(sp["id"], job["language"]),
            "talk_time": sp["talk_time"],
            "share": round(sp["talk_time"] / total, 3),
            "first_start": sp["first_start"],
        }
        for sp in speakers
    ]


@router.get("/jobs/{job_id}/speakers", summary="Intervenants détectés")
def list_speakers(job_id: str):
    return speakers_out(job_or_404(job_id))
