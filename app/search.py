"""Recherche dans les transcripts terminés, sans index.

Les transcripts sont relus à chaque requête ; leur découpage en unités est gardé en mémoire tant que
result.json ne change pas (une nouvelle diarisation le réécrit). Quelques centaines de réunions se
parcourent ainsi en une fraction de seconde, sans table à tenir à jour.

Comparaison insensible à la casse, aux accents, aux ligatures (œ, æ) et aux apostrophes typographiques.
Un paragraphe du transcript correspond s'il contient tous les termes ; "entre guillemets" ou « entre
chevrons », une expression est cherchée telle quelle.
"""

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from . import db
from .config import get_settings
from .jsonio import read_json
from .render import Unit, build_units, display_title, fmt_ts, group_turns, speaker_names

FOLD_EXTRA = str.maketrans({"’": "'", "‘": "'", "œ": "oe", "Œ": "oe", "æ": "ae", "Æ": "ae"})
QUERY_PATTERN = re.compile(r'"([^"]*)"|«([^»]*)»|(\S+)')
MAX_TERMS = 10
SNIPPET_BEFORE = 60   # caractères gardés avant la première occurrence
SNIPPET_AFTER = 140   # et après son début


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.translate(FOLD_EXTRA))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def parse_query(query: str) -> list[str]:
    """Termes repliés, sans doublon ; une expression entre guillemets forme un seul terme."""
    terms: list[str] = []
    for groups in QUERY_PATTERN.findall(query):
        term = " ".join(fold("".join(groups)).split())  # un seul des trois groupes est renseigné
        if term and term not in terms:
            terms.append(term)
    return terms[:MAX_TERMS]


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Texte replié et, pour chacun de ses caractères, l'indice du caractère d'origine."""
    out, origin = [], []
    for i, char in enumerate(text):
        folded = fold(char)
        out.append(folded)
        origin.extend([i] * len(folded))
    return "".join(out), origin


def highlight_ranges(text: str, terms: list[str]) -> list[list[int]]:
    """Occurrences des termes dans `text`, en positions [début, fin[ du texte d'origine, fusionnées."""
    folded, origin = _fold_with_map(text)
    ranges = []
    for term in terms:
        start = folded.find(term)
        while start >= 0:
            ranges.append([origin[start], origin[start + len(term) - 1] + 1])
            start = folded.find(term, start + len(term))
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def snippet(text: str, terms: list[str]) -> tuple[str, list[list[int]]]:
    """Extrait autour de la première occurrence, coupé entre deux mots, et ses surlignages."""
    ranges = highlight_ranges(text, terms)
    first = ranges[0][0] if ranges else 0
    start, end = max(0, first - SNIPPET_BEFORE), min(len(text), first + SNIPPET_AFTER)
    if start > 0:
        space = text.find(" ", start, first)
        start = space + 1 if space >= 0 else start
    if end < len(text):
        space = text.rfind(" ", max(first, ranges[0][1] if ranges else first), end)
        end = space if space > 0 else end
    prefix = "… " if start > 0 else ""
    shift = len(prefix) - start
    kept = [[max(a, start) + shift, min(b, end) + shift] for a, b in ranges if a < end and b > start]
    return prefix + text[start:end] + (" …" if end < len(text) else ""), kept


@lru_cache(maxsize=512)
def _units(path: Path, mtime_ns: int) -> tuple[Unit, ...]:
    return tuple(build_units(read_json(path)["segments"]))


def search(query: str, per_job: int = 3, limit: int = 50) -> dict:
    """Réunions terminées dont un paragraphe (ou le titre) contient tous les termes, de la plus récente
    à la plus ancienne ; pour chacune, le nombre de passages trouvés et les `per_job` premiers."""
    terms = parse_query(query)
    settings = get_settings()
    items: list[dict] = []
    total = 0
    truncated = False
    jobs, _ = db.list_jobs(limit=100_000, status="completed") if terms else ([], 0)
    for job in jobs:
        path = settings.job_dir(job["id"]) / "result.json"
        try:
            mtime = path.stat().st_mtime_ns
        except FileNotFoundError:
            continue
        names = speaker_names(job, db.get_speakers(job["id"]))
        found = [(turn.name, para) for turn in group_turns(list(_units(path, mtime)), names, job["language"])
                 for para in turn.paragraphs
                 if all(term in fold(" ".join(para.texts)) for term in terms)]
        title = display_title(job)
        title_match = all(term in fold(title) for term in terms)
        if not found and not title_match:
            continue
        if len(items) == limit:
            truncated = True
            break
        hits = []
        for name, para in found[:per_job]:
            text, ranges = snippet(" ".join(para.texts), terms)
            hits.append({"start": round(para.start, 2), "timestamp": fmt_ts(para.start), "speaker": name,
                         "text": text, "highlights": ranges})
        total += len(found)
        items.append({
            "job_id": job["id"],
            "title": title,
            "title_highlights": highlight_ranges(title, terms),
            "meeting_date": job.get("meeting_date"),
            "duration": job.get("duration"),
            "hit_count": len(found),
            "hits": hits,
        })
    return {"query": query, "terms": terms, "total_hits": total, "truncated": truncated, "items": items}
