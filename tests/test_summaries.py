import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import get_settings
from app.llm import base

from .conftest import upload, wait_for


class FakeLLM(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict, dict]] = []
    fail = False

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


def test_num_ctx():
    assert base.compute_num_ctx("a", "b", 65536) == 9216
    long_text = "x" * 350_000  # ~100k tokens
    ctx = base.compute_num_ctx("", long_text, 262144)
    assert ctx % 1024 == 0 and ctx > 100_000 + base.OUTPUT_RESERVE
    with pytest.raises(base.LLMError, match="OLLAMA_MAX_CTX"):
        base.compute_num_ctx("", long_text, 65536)


def test_strip_think():
    assert base.strip_think("<think>\nraisonnement\n</think>\n\nRéponse") == "Réponse"


def test_summary_with_ollama(llm_client, audio_file):
    client, fake = llm_client
    job_id = wait_for(client, upload(client, audio_file, title="Point hebdo")["id"])["id"]
    client.put(f"/api/jobs/{job_id}/speakers", json={"S1": "Alice"})

    resp = client.post(f"/api/jobs/{job_id}/summaries", json={"meeting_prompt": "Insiste sur le budget."})
    assert resp.status_code == 202
    summary = wait_summary(client, resp.json()["id"])
    assert summary["status"] == "completed", summary["error"]
    assert summary["content"] == "## Contexte\nRéunion test."
    assert summary["model"] == "qwen-test"
    assert summary["prompt_name"] == "Compte rendu de réunion"

    path, payload, _ = fake.requests[-1]
    assert path == "/api/chat"
    assert payload["think"] is False and payload["stream"] is False
    assert payload["options"]["num_ctx"] >= 8192
    system, user = payload["messages"]
    assert "compte rendu" in system["content"]
    assert user["content"].startswith("## Consignes propres à cette réunion\n\nInsiste sur le budget.")
    assert "**Alice** [00:00:00]" in user["content"]

    md = client.get(f"/api/summaries/{summary['id']}.md")
    assert md.status_code == 200
    meta = yaml.safe_load(md.text.split("---\n")[1])
    assert meta["title"] == "Compte rendu — Point hebdo"
    assert "point-hebdo_compte-rendu.md" in md.headers["content-disposition"]
    assert [s["id"] for s in client.get(f"/api/jobs/{job_id}/summaries").json()] == [summary["id"]]


def test_summary_with_openai_compatible_api(llm_client, audio_file):
    client, fake = llm_client
    job_id = wait_for(client, upload(client, audio_file)["id"])["id"]
    resp = client.post(f"/api/jobs/{job_id}/summaries",
                       json={"provider": "openai", "system_prompt": "Résume en une phrase."})
    summary = wait_summary(client, resp.json()["id"])
    assert summary["status"] == "completed", summary["error"]
    assert summary["content"] == "## Contexte\nVia API."
    assert summary["prompt_name"] == "(ponctuel)"
    path, payload, headers = fake.requests[-1]
    assert path == "/v1/chat/completions"
    assert payload["model"] == "gpt-test"
    assert headers["Authorization"] == "Bearer sk-test"
    assert payload["messages"][0]["content"] == "Résume en une phrase."


def test_summary_failure_is_reported(llm_client, audio_file):
    client, fake = llm_client
    job_id = wait_for(client, upload(client, audio_file)["id"])["id"]
    fake.fail = True
    summary = wait_summary(client, client.post(f"/api/jobs/{job_id}/summaries", json={}).json()["id"])
    assert summary["status"] == "failed"
    assert "500" in summary["error"]
    assert client.get(f"/api/summaries/{summary['id']}.md").status_code == 409


def test_openai_provider_requires_configuration(client, audio_file):
    job_id = wait_for(client, upload(client, audio_file)["id"])["id"]
    resp = client.post(f"/api/jobs/{job_id}/summaries", json={"provider": "openai"})
    assert resp.status_code == 400


def test_prompts_crud(client):
    prompts = client.get("/api/prompts").json()
    assert len(prompts) == 1 and prompts[0]["is_default"] == 1
    first = prompts[0]["id"]

    created = client.post("/api/prompts", json={"name": "Technique", "content": "…", "is_default": True}).json()
    prompts = client.get("/api/prompts").json()
    assert prompts[0]["id"] == created["id"] and prompts[0]["is_default"] == 1
    assert client.get(f"/api/prompts/{first}").json()["is_default"] == 0

    updated = client.put(f"/api/prompts/{first}", json={"name": "CR", "content": "Nouveau"}).json()
    assert updated["content"] == "Nouveau"

    assert client.delete(f"/api/prompts/{created['id']}").status_code == 204
    # Le prompt restant redevient le prompt par défaut
    assert client.get(f"/api/prompts/{first}").json()["is_default"] == 1
    assert client.delete(f"/api/prompts/{first}").status_code == 409
