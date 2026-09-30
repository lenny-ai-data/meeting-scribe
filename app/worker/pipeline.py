"""Point d'entrée du sous-processus de transcription : `python -m app.worker.pipeline <task_id>`.

Un sous-processus par tâche : à sa sortie, toute la VRAM est rendue. Il écrit lui-même
sa progression et son résultat en base ; le worker parent ne gère que l'échec brutal
(code de sortie non nul) et l'annulation.
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db, media
from ..config import get_settings
from ..errors import ScribeError
from .engine import Engine, make_engine

log = logging.getLogger("pipeline")


class PipelineError(ScribeError):
    pass


class Reporter:
    """Écrit l'étape et la progression du job en base (au plus une écriture par seconde)."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._last_write = 0.0
        self._last_value = -1.0

    def stage(self, status: str) -> None:
        log.info("Étape : %s", status)
        db.update_job(self.job_id, status=status, progress=0)
        self._last_value = 0.0

    def progress(self, percent: float) -> None:
        now = time.monotonic()
        value = round(min(max(percent, 0.0), 100.0), 1)
        if value > self._last_value and (now - self._last_write >= 1.0 or value >= 100.0):
            db.update_job(self.job_id, progress=value)
            self._last_write, self._last_value = now, value


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def run_transcribe(job: dict, task: dict, rep: Reporter) -> None:
    settings = get_settings()
    job_dir = settings.job_dir(job["id"])
    job_dir.mkdir(parents=True, exist_ok=True)

    if job["source_url"] and not job["source_path"]:
        from .. import youtube

        rep.stage("downloading")
        source, info = youtube.download(job["source_url"], job_dir, settings.cookies_file, rep.progress)
        fields = {"source_path": source.name, "source_name": info.get("source_name") or source.name}
        if not job["title"] and info.get("title"):
            fields["title"] = info["title"]
        if not job["meeting_date"] and info.get("date"):
            fields["meeting_date"] = info["date"]
        db.update_job(job["id"], **fields)
        job = db.get_job(job["id"])

    rep.stage("preparing")
    source = job_dir / job["source_path"]
    info = media.probe(source)
    if not info["has_audio"]:
        raise PipelineError("Le fichier ne contient pas de piste audio.")
    wav = job_dir / "audio.wav"
    media.to_wav(source, wav)
    duration = media.probe(wav)["duration"] or info["duration"]
    fields = {"duration": duration}
    if not job["meeting_date"]:
        tz = ZoneInfo(settings.tz)
        recorded = info["creation_time"] or datetime.fromisoformat(job["created_at"])
        fields["meeting_date"] = recorded.astimezone(tz).isoformat(timespec="seconds")
    db.update_job(job["id"], **fields)

    engine = make_engine(settings, job["device"])
    engine.prepare_gpu(check_vram=True)
    audio = engine.load_audio(wav)

    rep.stage("transcribing")
    raw = engine.transcribe(audio, job["model"], job["language"], job["vocabulary"], rep.progress)
    write_json(job_dir / "transcription.json", raw)

    engine.prepare_gpu(check_vram=False)
    rep.stage("aligning")
    aligned = engine.align(raw, audio, job["language"], rep.progress)
    write_json(job_dir / "aligned.json", aligned)

    diarize_and_finish(job, engine, audio, aligned, rep)


def run_rediarize(job: dict, task: dict, rep: Reporter) -> None:
    settings = get_settings()
    job_dir = settings.job_dir(job["id"])
    aligned_path = job_dir / "aligned.json"
    if not aligned_path.exists():
        raise PipelineError("Transcription alignée introuvable : relancez une transcription complète.")
    engine = make_engine(settings, job["device"])
    engine.prepare_gpu(check_vram=True)
    audio = engine.load_audio(job_dir / "audio.wav")
    diarize_and_finish(job, engine, audio, read_json(aligned_path), rep)


def diarize_and_finish(job: dict, engine: Engine, audio, aligned: dict, rep: Reporter) -> None:
    from ..speakers import finalize

    settings = get_settings()
    job_dir = settings.job_dir(job["id"])
    rep.stage("diarizing")
    diarization = engine.diarize(audio, job["num_speakers"], job["min_speakers"], job["max_speakers"], rep.progress)
    result = engine.assign_speakers(diarization, aligned)
    finalize(job, job_dir, diarization, result)
    db.update_job(job["id"], status="completed", progress=100, error=None, finished_at=db.now_iso())


RUNNERS = {"transcribe": run_transcribe, "rediarize": run_rediarize}


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    task = db.get_task(int(argv[1]))
    job = db.get_job(task["job_id"])
    log.info("Tâche %s (%s) du job %s", task["id"], task["kind"], job["id"])
    try:
        RUNNERS[task["kind"]](job, task, Reporter(job["id"]))
    except Exception as exc:
        log.exception("Échec de la tâche")
        message = str(exc) if isinstance(exc, ScribeError) else f"{type(exc).__name__}: {exc}"
        if task["kind"] == "rediarize":
            # Le résultat précédent reste valable
            db.update_job(job["id"], status="completed", progress=100,
                          error=f"Nouvelle diarisation en échec : {message}", finished_at=db.now_iso())
        else:
            db.update_job(job["id"], status="failed", error=message, finished_at=db.now_iso())
        db.finish_task(task["id"], "failed", message)
        return 1
    db.finish_task(task["id"], "done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
