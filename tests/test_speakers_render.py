import yaml

from app import render, speakers

from .conftest import upload, wait_for


# --- Fonctions pures ---------------------------------------------------------------

def test_subtract_removes_overlaps():
    assert speakers.subtract([(0, 10)], [(2, 3), (8, 12)]) == [(0, 2), (3, 8)]
    assert speakers.subtract([(0, 5)], [(0, 5)]) == []


def test_pick_samples_spread_and_bounds():
    clean = [(1, 3), (5, 30), (40, 44), (60, 61), (70, 76), (95, 99)]
    chosen = speakers.pick_samples(clean, clean, duration=100)
    assert len(chosen) == 3
    assert all(speakers.MIN_CLIP <= e - s <= speakers.MAX_CLIP for s, e in chosen)
    # Un extrait par tiers de la réunion
    thirds = sorted({int(((s + e) / 2) // (100 / 3)) for s, e in chosen})
    assert thirds == [0, 1, 2]


def test_pick_samples_fallback_when_never_alone():
    assert speakers.pick_samples([], [(10, 11.5)], duration=60) == [(10, 11.5)]
    assert speakers.pick_samples([], [(10, 10.5)], duration=60) == []


def test_mapping_by_first_appearance():
    diar = [{"start": 5, "end": 6, "speaker": "SPEAKER_01"}, {"start": 0, "end": 4, "speaker": "SPEAKER_03"}]
    assert speakers.build_mapping(diar, []) == {"SPEAKER_03": "S1", "SPEAKER_01": "S2"}


def _seg(start, end, words):
    return {"start": start, "end": end, "text": " ".join(w for w, *_ in words),
            "words": [{"word": w, "start": s, "end": e, "speaker": sp} for w, s, e, sp in words]}


def test_units_split_on_speaker_change_and_smooth_isolated_word():
    seg = _seg(0, 6, [
        ("Bonjour", 0, 0.5, "S1"), ("à", 0.5, 0.7, "S1"), ("tous", 0.7, 1.0, "S2"), ("et", 1.0, 1.2, "S1"),
        ("bienvenue.", 1.2, 2.0, "S1"), ("Merci", 3.0, 3.5, "S2"), ("beaucoup.", 3.5, 4.0, "S2"),
    ])
    units = render.build_units([seg])
    assert [(u.speaker, u.text) for u in units] == [
        ("S1", "Bonjour à tous et bienvenue."),
        ("S2", "Merci beaucoup."),
    ]


def test_units_words_without_timing_inherit():
    seg = {"start": 0, "end": 3, "speaker": "S1", "text": "Il y a 25 personnes",
           "words": [{"word": "Il", "start": 0, "end": 0.2, "speaker": "S1"}, {"word": "y"}, {"word": "a"},
                     {"word": "25"}, {"word": "personnes", "start": 1.0, "end": 1.5, "speaker": "S1"}]}
    assert [u.text for u in render.build_units([seg])] == ["Il y a 25 personnes"]


def test_render_merges_same_name_and_paragraphs():
    segments = [
        _seg(0, 2, [("Salut", 0, 1, "S1")]),
        _seg(2, 4, [("Oui", 2, 3, "S3")]),       # S3 et S1 portent le même nom → fusion
        _seg(10, 12, [("Ensuite", 10, 11, "S1")]),  # pause > 3 s → nouveau paragraphe
        _seg(12, 14, [("Bonjour", 12, 13, "S2")]),
    ]
    job = {"id": "abc", "title": "Point hebdo", "language": "fr", "model": "large-v3", "duration": 3725.4,
           "meeting_date": "2026-10-01T14:30:00+02:00", "source_name": "reunion.m4a", "source_url": None}
    sp = [{"id": "S1", "name": "Alice"}, {"id": "S2", "name": None}, {"id": "S3", "name": "Alice"}]
    md = render.render_transcript(job, segments, sp, "pyannote/speaker-diarization-community-1")

    _, header, body = md.split("---\n", 2)
    meta = yaml.safe_load(header)
    assert meta["title"] == "Point hebdo"
    assert meta["date"] == "2026-10-01T14:30:00+02:00"
    assert meta["duration"] == "01:02:05"
    assert meta["duration_seconds"] == 3725
    assert meta["speakers"] == ["Alice", "Intervenant 2"]
    assert meta["source_file"] == "reunion.m4a"
    assert body.split("\n\n") == [
        "\n# Point hebdo",
        "**Alice** [00:00:00]\nSalut Oui",
        "[00:00:10] Ensuite",
        "**Intervenant 2** [00:00:12]\nBonjour\n",
    ]


def test_suggested_filename():
    job = {"id": "x", "title": "Réunion équipe / budget", "meeting_date": "2026-10-01T14:30:00+02:00"}
    assert render.suggested_filename(job) == "2026-10-01_14h30_reunion-equipe-budget.md"
    assert render.suggested_filename({"id": "x", "source_name": "memo.m4a"}, "_compte-rendu") == "memo_compte-rendu.md"


# --- API ------------------------------------------------------------------------------

def test_speakers_api_flow(client, audio_file):
    job = wait_for(client, upload(client, audio_file, title="Point hebdo")["id"])
    job_id = job["id"]

    sps = client.get(f"/api/jobs/{job_id}/speakers").json()
    assert [s["id"] for s in sps] == ["S1", "S2"]
    assert sps[0]["samples"], "au moins un extrait"
    sample = sps[0]["samples"][0]
    assert sample["text"].startswith("Phrase")
    clip = client.get(sample["url"])
    assert clip.status_code == 200 and clip.headers["content-type"] == "audio/mpeg"

    md = client.get(f"/api/jobs/{job_id}/transcript.md")
    assert md.status_code == 200
    assert "**Intervenant 1** [00:00:00]" in md.text
    assert "2026-09-30_14h30_point-hebdo.md" in md.headers["content-disposition"]

    resp = client.put(f"/api/jobs/{job_id}/speakers", json={"S1": " Alice ", "S2": "Alice"})
    assert resp.status_code == 200
    md = client.get(f"/api/jobs/{job_id}/transcript.md").text
    # Même nom → un seul tour de parole
    assert md.count("**Alice**") == 1
    assert "speakers:\n- Alice\n" in md

    assert client.put(f"/api/jobs/{job_id}/speakers", json={"S9": "X"}).status_code == 422
    assert client.get("/api/people").json() == [{"name": "Alice", "jobs": 1}]

    data = client.get(f"/api/jobs/{job_id}/transcript.json").json()
    assert data["speakers"] == [{"id": "S1", "name": "Alice"}, {"id": "S2", "name": "Alice"}]


def test_rediarize(client, audio_file):
    job_id = wait_for(client, upload(client, audio_file)["id"])["id"]
    client.put(f"/api/jobs/{job_id}/speakers", json={"S1": "Alice"})
    resp = client.post(f"/api/jobs/{job_id}/rediarize", json={"num_speakers": 3})
    assert resp.status_code == 202
    job = wait_for(client, job_id)
    assert job["status"] == "completed"
    assert job["num_speakers"] == 3
    assert [s["id"] for s in job["speakers"]] == ["S1", "S2", "S3"]
    # Nouveaux libellés : les anciens noms ne s'appliquent plus
    assert all(s["name"] is None for s in job["speakers"])


def test_transcript_requires_completed_job(client, tmp_path):
    bogus = tmp_path / "x.m4a"
    bogus.write_text("rien")
    job = wait_for(client, upload(client, bogus)["id"])
    assert client.get(f"/api/jobs/{job['id']}/transcript.md").status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/rediarize", json={}).status_code == 409
