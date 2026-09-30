import json
import os
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import callbacks

from .conftest import upload, wait_for


class Hook(BaseHTTPRequestHandler):
    received: list[tuple[dict, dict]] = []
    status = 200

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((payload, dict(self.headers)))
        self.send_response(self.status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def hook(monkeypatch):
    Hook.received, Hook.status = [], 200
    monkeypatch.setattr(callbacks, "RETRY_DELAYS", (0.1, 0.1, 0.1))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Hook, f"http://127.0.0.1:{server.server_port}/webhook/scribe"
    server.shutdown()


def wait_events(hook, count, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(hook.received) < count:
        time.sleep(0.1)
    return [p for p, _ in hook.received]


def test_callbacks_for_n8n(client, audio_file, hook, settings_env):
    fake, url = hook
    job = upload(client, audio_file, callback_url=url, external_ref="drive-file-123", source_name="Point hebdo.m4a")
    wait_for(client, job["id"])
    [completed] = wait_events(fake, 1)
    assert completed["event"] == "job.completed"
    assert completed["external_ref"] == "drive-file-123"
    assert completed["suggested_filename"] == "2026-09-30_14h30_point-hebdo.md"
    assert completed["markdown"].startswith("---\n")
    assert completed["speakers"][0] == {"id": "S1", "name": "Intervenant 1", "talk_time": completed["speakers"][0]["talk_time"]}
    assert fake.received[0][1]["X-Scribe-Event"] == "job.completed"

    client.put(f"/api/jobs/{job['id']}/speakers", json={"S1": "Alice"})
    events = wait_events(fake, 2)
    assert events[1]["event"] == "job.speakers_updated"
    assert "**Alice**" in events[1]["markdown"]
    assert client.get(f"/api/jobs/{job['id']}").json()["callback_status"].startswith("job.speakers_updated : HTTP 200")


def test_callback_on_failure_and_retries(client, tmp_path, hook):
    fake, url = hook
    fake.status = 500
    bogus = tmp_path / "x.m4a"
    bogus.write_text("rien")
    job = wait_for(client, upload(client, bogus, callback_url=url)["id"])
    events = wait_events(fake, 4)
    assert [e["event"] for e in events] == ["job.failed"] * 4
    assert "markdown" not in events[0] and events[0]["error"]
    deadline = time.monotonic() + 5
    while "échec" not in (client.get(f"/api/jobs/{job['id']}").json()["callback_status"] or ""):
        assert time.monotonic() < deadline
        time.sleep(0.1)


@pytest.fixture
def fake_ytdlp(tmp_path, settings_env):
    """Faux yt-dlp : fabrique un fichier audio et son .info.json comme le vrai."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "yt-dlp"
    script.write_text("""#!/bin/sh
for last; do :; done
case "$last" in *fail*) echo "ERROR: [youtube] abc: Sign in to confirm you're not a bot"; exit 1;; esac
while [ "$1" != "-o" ]; do shift; done
out=$(echo "$2" | sed 's/%(ext)s/webm/')
dir=$(dirname "$out")
echo "[download]  50.0% of 1.00MiB"
ffmpeg -nostdin -loglevel error -y -f lavfi -i sine=duration=12 -c:a libopus "$out"
echo '{"title": "Conférence de presse", "timestamp": 1790000000, "webpage_url": "https://youtu.be/x"}' > "$dir/source.info.json"
echo "[download] 100% of 1.00MiB"
echo "$out"
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    settings_env.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return script


def test_youtube_url_job(fake_ytdlp, client):
    resp = client.post("/api/jobs", data={"url": "https://www.youtube.com/watch?v=abc"})
    assert resp.status_code == 202
    job = wait_for(client, resp.json()["id"])
    assert job["status"] == "completed", job["error"]
    assert job["title"] == "Conférence de presse"
    assert job["source_name"] == "Conférence de presse.webm"
    assert job["source_path"] == "source.webm"
    assert job["meeting_date"] == "2026-09-21T16:13:20+02:00"
    md = client.get(f"/api/jobs/{job['id']}/transcript.md").text
    assert "source_url: https://www.youtube.com/watch?v=abc" in md


def test_youtube_failure_message(fake_ytdlp, client):
    resp = client.post("/api/jobs", data={"url": "https://www.youtube.com/watch?v=fail"})
    job = wait_for(client, resp.json()["id"])
    assert job["status"] == "failed"
    assert "Sign in" in job["error"] and "youtube-cookies.txt" in job["error"]
