import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    """Données dans un dossier temporaire, pipeline factice. Hérité par le sous-processus du pipeline."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FAKE_PIPELINE", "true")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:9")  # injoignable
    monkeypatch.setenv("DIARIZATION_MODEL_DIR", str(tmp_path / "pyannote"))  # pas de modèle embarqué
    for name in ("API_TOKEN", "CALLBACK_TOKEN", "PUBLIC_BASE_URL", "LLM_API_BASE_URL", "LLM_API_KEY",
                 "LLM_API_MODEL", "HF_TOKEN", "MIN_FREE_VRAM_GB"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.fixture
def client(settings_env):
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(scope="session")
def audio_file(tmp_path_factory) -> Path:
    """30 s d'audio synthétique au format .m4a (AAC), comme un mémo vocal d'iPhone."""
    path = tmp_path_factory.mktemp("audio") / "reunion test.m4a"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-ac", "1", "-c:a", "aac", "-metadata", "creation_time=2026-09-30T12:30:00Z", str(path)],
        check=True,
    )
    return path


def wait_for(client: TestClient, job_id: str, statuses=("completed", "failed", "cancelled"), timeout=60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"Job {job_id} toujours en statut {job['status']} après {timeout} s")


def upload(client: TestClient, audio_file: Path, **fields) -> dict:
    with open(audio_file, "rb") as fh:
        resp = client.post("/api/jobs", files={"file": (audio_file.name, fh, "audio/mp4")}, data=fields)
    assert resp.status_code == 202, resp.text
    return resp.json()
