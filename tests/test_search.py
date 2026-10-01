from app.search import fold, highlight_ranges, parse_query, snippet

from .conftest import upload, wait_for


def test_fold_and_query():
    assert fold("Œuvre À L’ÉTÉ") == "oeuvre a l'ete"
    assert parse_query('Budget  "Plan  d’action" «  été 2026 » budget') == ["budget", "plan d'action", "ete 2026"]
    assert parse_query('"" ') == []


def test_highlights_map_back_to_original_text():
    text = "L’été, l'Été et l’œuvre."
    ranges = highlight_ranges(text, parse_query("l'ete oeuvre"))
    assert [text[a:b] for a, b in ranges] == ["L’été", "l'Été", "œuvre"]


def test_snippet_cuts_between_words():
    text = " ".join(f"mot{i}" for i in range(100)) + " cible " + " ".join(f"fin{i}" for i in range(100))
    extract, ranges = snippet(text, ["cible"])
    assert extract.startswith("… mot") and extract.endswith(" …")
    assert [extract[a:b] for a, b in ranges] == ["cible"]
    assert len(extract) < 230


def test_search_transcripts(client, audio_file):
    job = wait_for(client, upload(client, audio_file, title="Point budget")["id"])
    assert job["status"] == "completed"

    # Accents et casse ignorés ; tous les termes dans un même paragraphe (deux phrases par tour)
    data = client.get("/api/search", params={"q": "NUMERO 3"}).json()
    assert data["total_hits"] == 1
    item = data["items"][0]
    assert item["job_id"] == job["id"] and item["title"] == "Point budget"
    hit = item["hits"][0]
    assert hit["timestamp"] == "00:00:08" and hit["speaker"] == "Intervenant 2"
    assert [hit["text"][a:b] for a, b in hit["highlights"]] == ["numéro", "3", "numéro"]

    # Expression exacte
    data = client.get("/api/search", params={"q": '"numéro 5 de"'}).json()
    assert [h["timestamp"] for h in data["items"][0]["hits"]] == ["00:00:16"]

    # Tous les passages sont comptés, `per_job` premiers renvoyés
    item = client.get("/api/search", params={"q": "phrase", "per_job": 2}).json()["items"][0]
    assert item["hit_count"] == 4 and len(item["hits"]) == 2

    # Titre seul
    item = client.get("/api/search", params={"q": "budget"}).json()["items"][0]
    assert item["hit_count"] == 0 and item["title_highlights"] == [[6, 12]]

    # Les noms courants des intervenants
    client.put(f"/api/jobs/{job['id']}/speakers", json={"S2": "Claire"})
    hit = client.get("/api/search", params={"q": "numéro 3"}).json()["items"][0]["hits"][0]
    assert hit["speaker"] == "Claire"

    assert client.get("/api/search", params={"q": "introuvable"}).json()["items"] == []
    assert client.get("/api/search", params={"q": "a"}).status_code == 422


def test_search_skips_unfinished_jobs(client, audio_file):
    from app import db

    job = wait_for(client, upload(client, audio_file, title="Budget")["id"])
    assert client.get("/api/search", params={"q": "budget"}).json()["items"]
    db.update_job(job["id"], status="failed")
    assert client.get("/api/search", params={"q": "budget"}).json()["items"] == []
