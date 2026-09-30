import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.worker import gpu


class FakeOllama(BaseHTTPRequestHandler):
    loaded: list[str] = []
    calls: list[tuple[str, dict]] = []
    stuck = False

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/ps":
            self._reply({"models": [{"name": n} for n in self.loaded]})
        else:
            self._reply({}, 404)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, payload))
        if self.path == "/api/generate" and payload["model"].startswith("embed"):
            return self._reply({"error": "does not support generate"}, 400)
        if payload.get("keep_alive") == 0 and not self.stuck:
            type(self).loaded = [n for n in self.loaded if n != payload["model"]]
        self._reply({"done": True})

    def log_message(self, *args):
        pass


@pytest.fixture
def ollama():
    FakeOllama.loaded, FakeOllama.calls, FakeOllama.stuck = [], [], False
    server = HTTPServer(("127.0.0.1", 0), FakeOllama)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield FakeOllama, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_unload_all_models(ollama):
    fake, url = ollama
    fake.loaded = ["qwen3.8:27b", "embed-text:latest"]
    assert gpu.unload_ollama(url, timeout=5) == ["qwen3.8:27b", "embed-text:latest"]
    assert fake.loaded == []
    assert ("/api/embed", {"model": "embed-text:latest", "input": "", "keep_alive": 0}) in fake.calls


def test_nothing_loaded(ollama):
    fake, url = ollama
    assert gpu.unload_ollama(url, timeout=5) == []
    assert fake.calls == []


def test_ollama_unreachable():
    assert gpu.unload_ollama("http://127.0.0.1:9", timeout=1) == []


def test_ollama_refuses_to_unload(ollama):
    fake, url = ollama
    fake.loaded, fake.stuck = ["qwen3.8:27b"], True
    with pytest.raises(gpu.GpuBusyError, match="qwen3.8:27b"):
        gpu.unload_ollama(url, timeout=1.5)
