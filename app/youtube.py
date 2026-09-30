"""Téléchargement de l'audio d'une URL (YouTube…) avec yt-dlp.

yt-dlp a besoin d'un moteur JavaScript (Deno, installé dans l'image) et des scripts EJS
(`yt-dlp[default]`) pour passer les protections de YouTube.
"""

import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import ScribeError

log = logging.getLogger("youtube")

PROGRESS = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


class DownloadError(ScribeError):
    pass


def _meeting_date(info: dict) -> str | None:
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
    day = info.get("upload_date")
    if day and len(day) == 8:
        return f"{day[:4]}-{day[4:6]}-{day[6:]}"
    return None


def download(url: str, job_dir: Path, cookies: Path | None,
             on_progress: Callable[[float], None]) -> tuple[Path, dict]:
    """Télécharge la meilleure piste audio dans job_dir/source.<ext>. Renvoie le chemin et des métadonnées."""
    cmd = ["yt-dlp", "--no-playlist", "--newline", "--no-colors", "-f", "bestaudio/best",
           "--write-info-json", "-o", str(job_dir / "source.%(ext)s"),
           "--print", "after_move:filepath", url]
    if cookies and cookies.is_file():
        cmd[1:1] = ["--cookies", str(cookies)]
    log.info("yt-dlp %s", url)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines: list[str] = []
    path: Path | None = None
    for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        if m := PROGRESS.search(line):
            on_progress(float(m.group(1)))
        elif line.startswith("/") and Path(line).is_file():
            path = Path(line)
        elif line:
            log.info(line)
    proc.wait()

    if proc.returncode != 0 or path is None:
        errors = [l.removeprefix("ERROR: ") for l in lines if l.startswith("ERROR")] or lines[-3:]
        message = " ".join(errors)[:500] or f"code {proc.returncode}"
        hint = ""
        if "sign in" in message.lower() or "bot" in message.lower():
            hint = (" YouTube demande une connexion : exportez les cookies d'un navigateur connecté dans "
                    "data/config/youtube-cookies.txt (format Netscape).")
        raise DownloadError(f"Téléchargement impossible (yt-dlp) : {message}.{hint}")

    info_path = job_dir / "source.info.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    title = info.get("title")
    return path, {
        "title": title,
        "source_name": f"{title}{path.suffix}" if title else path.name,
        "date": _meeting_date(info),
        "webpage_url": info.get("webpage_url"),
    }
