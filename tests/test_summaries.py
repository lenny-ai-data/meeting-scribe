import pytest
import yaml

from app.llm import base

from .conftest import upload, wait_for, wait_summary


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

    # Retouche du compte rendu avant téléchargement
    edited = client.patch(f"/api/summaries/{summary['id']}", json={"content": "## Contexte\nTexte corrigé."})
    assert edited.status_code == 200 and edited.json()["content"] == "## Contexte\nTexte corrigé."
    assert client.get(f"/api/summaries/{summary['id']}.md").text.endswith("## Contexte\nTexte corrigé.\n")


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
    assert client.patch(f"/api/summaries/{summary['id']}", json={"content": "x"}).status_code == 409


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
