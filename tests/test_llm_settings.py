from app.llm.config import OllamaConfig

from .conftest import upload, wait_for, wait_summary


def test_defaults_come_from_env(llm_client):
    client, _ = llm_client
    data = client.get("/api/settings/llm").json()
    assert data["ollama"]["model"] == "qwen-test"
    assert data["openai"] == {"base_url": data["openai"]["base_url"], "model": "gpt-test",
                              "api_key_set": True, "configured": True}
    assert data["overrides"] == {"ollama": [], "openai": []}


def test_api_key_is_write_only(llm_client):
    client, _ = llm_client
    data = client.put("/api/settings/llm", json={"openai": {"api_key": "sk-secret-123"}}).json()
    assert data["openai"]["api_key_set"] is True
    for path in ("/api/settings/llm", "/api/system"):
        assert "sk-secret-123" not in client.get(path).text
    # Chaîne vide : plus de clé, même si le .env en fournit une
    data = client.put("/api/settings/llm", json={"openai": {"api_key": ""}}).json()
    assert data["openai"]["api_key_set"] is False


def test_partial_update_and_reset_to_env(llm_client):
    client, _ = llm_client
    data = client.put("/api/settings/llm", json={"ollama": {"model": "mistral-test", "max_ctx": 32768}}).json()
    assert data["ollama"]["model"] == "mistral-test"
    assert data["ollama"]["max_ctx"] == 32768
    assert data["overrides"]["ollama"] == ["max_ctx", "model"]
    # Un champ omis est conservé ; null reprend la valeur du .env
    data = client.put("/api/settings/llm", json={"ollama": {"model": None}}).json()
    assert data["ollama"]["model"] == "qwen-test"
    assert data["ollama"]["max_ctx"] == 32768


def test_invalid_url_rejected(llm_client):
    client, _ = llm_client
    resp = client.put("/api/settings/llm", json={"ollama": {"url": "192.168.1.20:11434"}})
    assert resp.status_code == 422


def test_summary_uses_stored_settings(llm_client, audio_file):
    client, fake = llm_client
    job = wait_for(client, upload(client, audio_file)["id"])
    good_url = client.get("/api/settings/llm").json()["ollama"]["url"]

    client.put("/api/settings/llm", json={"ollama": {"url": "http://127.0.0.1:9/"}})
    s = wait_summary(client, client.post(f"/api/jobs/{job['id']}/summaries", json={}).json()["id"])
    assert s["status"] == "failed" and "127.0.0.1:9" in s["error"]

    client.put("/api/settings/llm", json={"ollama": {"url": good_url, "model": "mistral-test"}})
    s = wait_summary(client, client.post(f"/api/jobs/{job['id']}/summaries", json={}).json()["id"])
    assert s["status"] == "completed" and s["model"] == "mistral-test"
    assert client.get("/api/system").json()["ollama"]["default_model"] == "mistral-test"


def test_summary_without_default_model(llm_client, audio_file):
    """Sans modèle par défaut, le premier modèle installé est utilisé, comme dans l'interface."""
    client, _ = llm_client
    job = wait_for(client, upload(client, audio_file)["id"])
    client.put("/api/settings/llm", json={"ollama": {"model": ""}})
    assert client.post(f"/api/jobs/{job['id']}/summaries", json={}).json()["model"] == "mistral-test"
    client.put("/api/settings/llm", json={"ollama": {"url": "http://127.0.0.1:9"}})
    resp = client.post(f"/api/jobs/{job['id']}/summaries", json={})
    assert resp.status_code == 400 and "injoignable" in resp.json()["detail"]


def test_list_and_test_models(llm_client):
    client, fake = llm_client
    assert client.get("/api/llm/models?provider=ollama").json()["models"] == ["mistral-test", "qwen-test"]
    assert client.get("/api/llm/models?provider=openai").json()["models"] == ["gpt-mini", "gpt-test"]
    assert ("/v1/models", {}) == fake.requests[-1][:2]
    assert fake.requests[-1][2]["Authorization"] == "Bearer sk-test"

    ok = client.post("/api/settings/llm/test", json={"provider": "openai", "api_key": "sk-autre"}).json()
    assert ok["ok"] is True and fake.requests[-1][2]["Authorization"] == "Bearer sk-autre"
    ko = client.post("/api/settings/llm/test", json={"provider": "ollama", "url": "http://127.0.0.1:9"}).json()
    assert ko["ok"] is False and "injoignable" in ko["error"]
    assert client.get("/api/llm/models?provider=ollama").status_code == 200


def test_unload_only_when_ollama_is_local():
    def cfg(url, forced=None):
        return OllamaConfig(url=url, model="", max_ctx=8192, unload_before_gpu=forced)

    assert cfg("http://host.docker.internal:11434").unload
    assert cfg("http://localhost:11434").unload
    assert not cfg("http://192.168.1.20:11434").unload
    assert cfg("http://192.168.1.20:11434", forced=True).unload
    assert not cfg("http://127.0.0.1:11434", forced=False).unload
