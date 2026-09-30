import threading

from app import db
from app.config import get_settings

from .conftest import upload, wait_for


def test_upload_runs_pipeline(client, audio_file):
    job = upload(client, audio_file, title="Point hebdo")
    assert job["status"] == "queued"
    assert job["source_name"] == "reunion test.m4a"

    job = wait_for(client, job["id"])
    assert job["status"] == "completed", job["error"]
    assert 29 < job["duration"] < 31
    # Date d'enregistrement lue dans les métadonnées, convertie en heure de Paris
    assert job["meeting_date"] == "2026-09-30T14:30:00+02:00"
    assert [sp["id"] for sp in job["speakers"]] == ["S1", "S2"]
    assert job["speakers"][0]["display_name"] == "Intervenant 1"

    job_dir = get_settings().job_dir(job["id"])
    for name in ("source.m4a", "audio.wav", "aligned.json", "result.json", "diarization.json"):
        assert (job_dir / name).exists(), name

    audio = client.get(f"/api/jobs/{job['id']}/audio", headers={"Range": "bytes=0-99"})
    assert audio.status_code == 206
    assert len(audio.content) == 100


def test_validation(client, audio_file):
    assert client.post("/api/jobs", data={}).status_code == 422
    with open(audio_file, "rb") as fh:
        resp = client.post("/api/jobs", files={"file": ("a.m4a", fh)}, data={"url": "https://youtu.be/x"})
    assert resp.status_code == 422
    assert client.post("/api/jobs", data={"url": "ftp://example.com/a"}).status_code == 422
    with open(audio_file, "rb") as fh:
        resp = client.post("/api/jobs", files={"file": ("a.m4a", fh)}, data={"model": "tiny"})
    assert resp.status_code == 422
    assert client.get("/api/jobs/nope").status_code == 404


def test_not_audio_fails_cleanly(client, tmp_path):
    bogus = tmp_path / "notes.m4a"
    bogus.write_text("pas de l'audio")
    job = upload(client, bogus)
    job = wait_for(client, job["id"])
    assert job["status"] == "failed"
    assert "ffprobe" in job["error"]


def test_jobs_run_one_at_a_time(client, audio_file, monkeypatch):
    # Espionne le nombre de sous-processus simultanés via le statut des tâches en base
    ids = [upload(client, audio_file)["id"] for _ in range(3)]
    max_running = 0
    stop = threading.Event()

    def watch():
        nonlocal max_running
        while not stop.is_set():
            with db.db() as conn:
                running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0]
            max_running = max(max_running, running)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        for job_id in ids:
            assert wait_for(client, job_id)["status"] == "completed"
    finally:
        stop.set()
        watcher.join()
    assert max_running == 1


def test_cancel_queued_and_delete(client, audio_file):
    first = upload(client, audio_file)
    second = upload(client, audio_file)
    resp = client.post(f"/api/jobs/{second['id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("cancelled", "completed")
    wait_for(client, first["id"])

    assert client.delete(f"/api/jobs/{first['id']}").status_code == 204
    assert client.get(f"/api/jobs/{first['id']}").status_code == 404
    assert not get_settings().job_dir(first["id"]).exists()


def test_list_and_system(client, audio_file):
    upload(client, audio_file)
    listing = client.get("/api/jobs").json()
    assert listing["total"] == 1
    info = client.get("/api/system").json()
    assert info["fake_pipeline"] is True
    assert info["ollama"]["reachable"] is False
    assert "large-v3-turbo" in info["options"]["models"]


def test_api_token(settings_env, audio_file):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings_env.setenv("API_TOKEN", "secret")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/jobs").status_code == 401
        assert c.get("/api/jobs", headers={"Authorization": "Bearer secret"}).status_code == 200
        assert c.get("/api/jobs?token=secret").status_code == 200


def test_cancel_running_job(settings_env, audio_file):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings_env.setenv("FAKE_PIPELINE_DELAY", "2")
    get_settings.cache_clear()
    with TestClient(create_app()) as c:
        job = upload(c, audio_file)
        wait_for(c, job["id"], statuses=("transcribing",))
        resp = c.post(f"/api/jobs/{job['id']}/cancel")
        assert resp.status_code == 200
        job = wait_for(c, job["id"], timeout=15)
        assert job["status"] == "cancelled"
        with db.db() as conn:
            assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "cancelled"
