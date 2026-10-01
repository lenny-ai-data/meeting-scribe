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
        resp = client.post("/api/jobs", files={"file": ("a.m4a", fh)}, data={"profile": "ultra"})
    assert resp.status_code == 422 and "tres_precis" in resp.json()["detail"]
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
    assert [p["id"] for p in info["options"]["profiles"]["cpu"]] == ["tres_rapide", "rapide", "precis", "tres_precis"]
    assert info["options"]["default_profiles"] == {"cuda": "tres_precis", "cpu": "rapide"}
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
    """Configuration la plus économe du modèle (int8, lots de 4) ou diarisation seule, plus 1 Go de marge."""
    settings = get_settings()
    assert settings.min_vram_gb("large-v3") == 4.9
    assert settings.min_vram_gb("large-v3-turbo") == 3.0
    assert settings.min_vram_gb("small") == 2.6  # la diarisation (1,6 Go) dépasse small en int8
    assert settings.min_vram_gb(None) == 2.6  # nouvelle diarisation seule
    # Seuil imposé par l'environnement ; une variable vide vaut « selon le modèle »
    settings_env.setenv("MIN_FREE_VRAM_GB", "10")
    get_settings.cache_clear()
    assert get_settings().min_vram_gb("small") == 10
    settings_env.setenv("MIN_FREE_VRAM_GB", "")
    get_settings.cache_clear()
    assert get_settings().min_vram_gb("large-v3") == 4.9


def test_gpu_config_follows_free_vram():
    from app.profiles import choose_gpu_config

    assert choose_gpu_config("large-v3", 22.0) == ("float16", 16)
    assert choose_gpu_config("large-v3", 7.5) == ("int8_float16", 8)  # carte de 8 Go
    assert choose_gpu_config("large-v3", 5.5) == ("int8_float16", 4)  # carte de 6 Go
    assert choose_gpu_config("large-v3-turbo", 5.5) == ("float16", 16)
    assert choose_gpu_config("small", 3.6) == ("float16", 16)
    assert choose_gpu_config("small", 3.2) == ("int8_float16", 16)  # carte de 4 Go avec affichage
    # BATCH_SIZE imposé : seule la précision s'adapte
    assert choose_gpu_config("large-v3", 22.0, batch_override=8) == ("float16", 8)
    assert choose_gpu_config("large-v3", 6.0, batch_override=8) == ("int8_float16", 8)


def test_profiles_set_model_and_diarization_step(client, audio_file):
    job = upload(client, audio_file, device="cpu")
    assert (job["profile"], job["model"], job["diarization_step"]) == ("rapide", "small", 2.5)
    job = upload(client, audio_file, device="cpu", profile="tres_rapide", num_speakers="3")
    assert (job["model"], job["diarization_step"], job["num_speakers"]) == ("small", 5.0, 3)
    job = upload(client, audio_file, device="cuda")
    assert (job["profile"], job["model"], job["diarization_step"]) == ("tres_precis", "large-v3", 1.0)
    job = wait_for(client, upload(client, audio_file, device="cuda", profile="rapide")["id"])
    assert job["model"] == "large-v3-turbo"
    md = client.get(f"/api/jobs/{job['id']}/transcript.md").text
    assert "profile: rapide" in md and "model: large-v3-turbo" in md


def test_small_card_greys_out_large_profiles(client, monkeypatch):
    from app.api import system

    async def gpu_4gb():
        return [{"name": "Petite carte", "memory_total_mb": 4096, "memory_used_mb": 0,
                 "memory_free_mb": 4096, "utilization_percent": 0}]

    monkeypatch.setattr(system, "_gpu", gpu_4gb)
    profiles = {p["id"]: p for p in client.get("/api/system").json()["options"]["profiles"]["cuda"]}
    assert profiles["tres_rapide"]["available"] and profiles["rapide"]["available"]
    assert not profiles["precis"]["available"] and not profiles["tres_precis"]["available"]
    assert profiles["precis"]["reason"] == "4,9 Go de VRAM requis, carte de 4,0 Go"


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


def test_progress_burst_is_not_lost(settings_env):
    """Une rafale d'avancées (un lot WhisperX) est écrite, pas seulement son premier point."""
    from app.worker.pipeline import Reporter

    db.init_db()
    job = db.create_job(status="queued", model="large-v3", language="fr", device="cpu")
    rep = Reporter(job["id"])
    rep.stage("transcribing")
    for k in range(1, 5):  # quatre segments signalés dans la même seconde
        rep.progress(k * 5)
    assert db.get_job(job["id"])["progress"] == 20
    rep.progress(20.2)  # petite avancée immédiate : différée
    assert db.get_job(job["id"])["progress"] == 20

    rep.detail("Téléchargement du modèle large-v3 (premier usage) : 1,2 Go sur 3,1 Go", 38.7)
    job_now = db.get_job(job["id"])
    assert job_now["progress"] == 38.7 and job_now["progress_detail"].startswith("Téléchargement")
    rep.detail("Chargement du modèle large-v3", 0)  # la barre peut repartir de zéro
    assert db.get_job(job["id"])["progress"] == 0
    rep.stage("aligning")
    assert db.get_job(job["id"])["progress_detail"] is None


def test_progress_detail_column_added_to_old_database(settings_env):
    db.init_db()
    with db.db() as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN progress_detail")
    db.init_db()
    with db.db() as conn:
        assert "progress_detail" in {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}


def test_cpu_batch_size(settings_env):
    assert get_settings().cpu_batch_size() == 4
    settings_env.setenv("BATCH_SIZE", "8")
    get_settings.cache_clear()
    assert get_settings().cpu_batch_size() == 8


def test_format_size():
    from app.worker.whisperx_engine import format_size

    assert format_size(850e6) == "850 Mo"
    assert format_size(1.62e9) == "1,6 Go"
