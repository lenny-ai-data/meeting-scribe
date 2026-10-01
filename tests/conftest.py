import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
                 "LLM_API_MODEL", "HF_TOKEN", "MIN_FREE_VRAM_GB", "BATCH_SIZE",
                 "DEFAULT_PROFILE", "DEFAULT_DEVICE"):
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


class FakeLLM(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, dict]] = []
    fail = False

    def do_GET(self):
        type(self).requests.append((self.path, {}, dict(self.headers)))
        if self.path == "/api/tags":
            self._reply({"models": [{"name": "qwen-test"}, {"name": "mistral-test"}]})
        elif self.path == "/v1/models":
            self._reply({"data": [{"id": "gpt-test"}, {"id": "gpt-mini"}]})
        else:
            self._reply({}, 404)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, payload, dict(self.headers)))
        if self.fail:
            return self._reply({"error": "boom"}, 500)
        if self.path == "/api/chat":
            self._reply({"message": {"role": "assistant", "content": "<think>hmm</think>\n## Contexte\nRéunion test."},
                         "prompt_eval_count": 1234, "eval_count": 56})
        elif self.path == "/v1/chat/completions":
            self._reply({"model": payload["model"], "choices": [{"message": {"content": "## Contexte\nVia API."}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        else:
            self._reply({}, 404)

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def llm_client(settings_env):
    FakeLLM.requests, FakeLLM.fail = [], False
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    settings_env.setenv("OLLAMA_URL", url)
    settings_env.setenv("OLLAMA_MODEL", "qwen-test")
    settings_env.setenv("LLM_API_BASE_URL", url + "/v1")
    settings_env.setenv("LLM_API_KEY", "sk-test")
    settings_env.setenv("LLM_API_MODEL", "gpt-test")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c, FakeLLM
    server.shutdown()


def wait_summary(client, summary_id, timeout=20):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = client.get(f"/api/summaries/{summary_id}").json()
        if s["status"] in ("completed", "failed", "cancelled"):
            return s
        time.sleep(0.1)
    raise AssertionError(s)
