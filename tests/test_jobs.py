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
    # Sans HF_TOKEN, le voyant est rouge ; Ollama injoignable n'est qu'un avertissement
    assert info["status"]["state"] == "error"
    assert any("HF_TOKEN" in p for p in info["status"]["problems"])
    assert any("Ollama" in w for w in info["status"]["warnings"])
    assert info["options"]["owner_name"] is None


def test_system_ready_with_token(settings_env):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings_env.setenv("HF_TOKEN", "hf_test")
    settings_env.setenv("OWNER_NAME", "Alice Martin")
    with TestClient(create_app()) as client:
        info = client.get("/api/system").json()
    assert info["status"] == {"state": "ready", "problems": [],
                              "warnings": ["Ollama injoignable : pas de compte rendu local"]}
    assert info["options"]["owner_name"] == "Alice Martin"


def test_system_ready_with_bundled_diarization(settings_env, tmp_path):
    """Modèle pyannote embarqué dans l'image : aucun jeton Hugging Face nécessaire."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    model_dir = tmp_path / "pyannote"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("pipeline: {}\n")
    settings = get_settings()
    assert settings.diarization_source == str(model_dir)
    with TestClient(create_app()) as client:
        info = client.get("/api/system").json()
    assert info["status"]["state"] == "ready"
    assert info["diarization"] == {"model": "pyannote/speaker-diarization-community-1", "bundled": True}


def test_diarization_source_without_bundle(settings_env):
    settings = get_settings()
    assert settings.diarization_source == "pyannote/speaker-diarization-community-1"
    assert not settings.diarization_ready
    settings_env.setenv("HF_TOKEN", "hf_test")
    get_settings.cache_clear()
    assert get_settings().diarization_ready


def test_min_vram_by_model(settings_env):
    settings = get_settings()
    assert settings.min_vram_gb("large-v3") == 10
    assert settings.min_vram_gb("large-v3-turbo") == 6
    assert settings.min_vram_gb("modèle inconnu") == 10
    # Seuil imposé par l'environnement ; une variable vide vaut « selon le modèle »
    settings_env.setenv("MIN_FREE_VRAM_GB", "4")
    get_settings.cache_clear()
    assert get_settings().min_vram_gb("large-v3") == 4
    settings_env.setenv("MIN_FREE_VRAM_GB", "")
    get_settings.cache_clear()
    assert get_settings().min_vram_gb("large-v3") == 10


def test_cpu_only_host(client, audio_file, monkeypatch):
    """Sans GPU, seul le CPU est proposé et une demande en cuda est refusée."""
    from app.api import jobs, system

    monkeypatch.setattr(jobs, "available_devices", lambda s: ("cpu",))
    monkeypatch.setattr(system, "available_devices", lambda s: ("cpu",))
    assert client.get("/api/system").json()["options"]["devices"] == ["cpu"]
    with open(audio_file, "rb") as fh:
        resp = client.post("/api/jobs", files={"file": (audio_file.name, fh, "audio/mp4")}, data={"device": "cuda"})
    assert resp.status_code == 422
    assert "cpu" in resp.json()["detail"]
    job = upload(client, audio_file, device="cpu")
    assert job["device"] == "cpu"


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


def test_patch_title_and_date(client, audio_file):
    job = wait_for(client, upload(client, audio_file)["id"])
    resp = client.patch(f"/api/jobs/{job['id']}", json={"title": "Comité", "meeting_date": "2026-10-02T09:00"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Comité"
    assert resp.json()["meeting_date"] == "2026-10-02T09:00:00+02:00"
    md = client.get(f"/api/jobs/{job['id']}/transcript.md")
    assert "2026-10-02_09h00_comite.md" in md.headers["content-disposition"]
    resp = client.patch(f"/api/jobs/{job['id']}", json={"title": ""})
    assert resp.json()["title"] is None
    assert resp.json()["display_title"] == "reunion test"
