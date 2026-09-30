"""File de tâches unique : une seule tâche à la fois, quel que soit son type.

C'est ce qui garantit qu'on ne charge jamais WhisperX/pyannote et le LLM en même temps sur la carte.
"""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable

from .. import db
from ..config import APP_ROOT, get_settings
from ..errors import ScribeError

log = logging.getLogger("worker")

PIPELINE_KINDS = ("transcribe", "rediarize")
TERMINATE_GRACE = 10.0


class Worker:
    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._current: dict | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._inner: asyncio.Task | None = None
        self._cancelled: set[int] = set()
        self._background: set[asyncio.Task] = set()
        # Tâches exécutées dans ce processus (ex. comptes rendus), par type
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        # Appelé après chaque tâche : (tâche, statut final)
        self.on_task_done: list[Callable[[dict, str], Awaitable[None]]] = []

    # --- API publique ---------------------------------------------------------

    @property
    def current(self) -> dict | None:
        return self._current

    def notify(self) -> None:
        self._wake.set()

    async def cancel_job(self, job_id: str) -> bool:
        """Annule les tâches en attente et la tâche en cours du job. Renvoie True si quelque chose a été annulé."""
        cancelled = False
        for task in db.queued_tasks(job_id):
            db.finish_task(task["id"], "cancelled")
            if task["summary_id"]:
                db.update_summary(task["summary_id"], status="cancelled", finished_at=db.now_iso())
            elif task["kind"] == "rediarize":
                # Le résultat précédent reste valable
                db.update_job(job_id, status="completed", progress=100)
            cancelled = True
        if self._current and self._current["job_id"] == job_id:
            await self._cancel_current()
            cancelled = True
        return cancelled

    async def cancel_summary(self, summary_id: str) -> bool:
        for task in db.queued_tasks():
            if task["summary_id"] == summary_id:
                db.finish_task(task["id"], "cancelled")
                db.update_summary(summary_id, status="cancelled", finished_at=db.now_iso())
                return True
        if self._current and self._current["summary_id"] == summary_id:
            await self._cancel_current()
            return True
        return False

    def spawn(self, coro: Awaitable) -> None:
        """Lance une coroutine en arrière-plan (callbacks…) en gardant une référence."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def run_forever(self) -> None:
        for task in db.recover_interrupted_tasks():
            await self._mark_failed(task, "Interrompue deux fois (redémarrage du service)")
        while True:
            task = db.claim_next_task()
            if task is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            await self._execute(task)

    async def shutdown(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._terminate(signal.SIGTERM)

    # --- Exécution --------------------------------------------------------------

    async def _execute(self, task: dict) -> None:
        self._current = task
        log.info("Tâche %s (%s) du job %s", task["id"], task["kind"], task["job_id"])
        try:
            if task["kind"] in PIPELINE_KINDS:
                status = await self._run_pipeline(task)
            else:
                status = await self._run_inline(task)
        except Exception as exc:
            log.exception("Tâche %s en échec", task["id"])
            message = str(exc) if isinstance(exc, ScribeError) else f"{type(exc).__name__}: {exc}"
            await self._mark_failed(task, message)
            status = "failed"
        finally:
            self._current = None
            self._proc = None
            self._inner = None
        for hook in self.on_task_done:
            self.spawn(hook(db.get_task(task["id"]) or task, status))

    async def _run_pipeline(self, task: dict) -> str:
        settings = get_settings()
        job_dir = settings.job_dir(task["job_id"])
        job_dir.mkdir(parents=True, exist_ok=True)
        db.update_job(task["job_id"], started_at=db.now_iso(), finished_at=None, error=None)
        with open(job_dir / "pipeline.log", "ab") as logfile:
            self._proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.worker.pipeline", str(task["id"]),
                cwd=APP_ROOT, stdout=logfile, stderr=logfile, start_new_session=True,
            )
            returncode = await self._proc.wait()

        if task["id"] in self._cancelled:
            self._cancelled.discard(task["id"])
            db.finish_task(task["id"], "cancelled")
            # Une rediarisation annulée laisse intact le résultat précédent
            status = "completed" if task["kind"] == "rediarize" else "cancelled"
            db.update_job(task["job_id"], status=status, progress=100 if status == "completed" else 0,
                          finished_at=db.now_iso())
            return "cancelled"
        final = db.get_task(task["id"])
        if final and final["status"] == "running":
            # Le sous-processus est mort sans rien écrire (OOM, segfault…)
            await self._mark_failed(task, f"Le processus de transcription s'est arrêté (code {returncode}). "
                                          f"Voir {job_dir / 'pipeline.log'}")
            return "failed"
        return final["status"] if final else "failed"

    async def _run_inline(self, task: dict) -> str:
        handler = self.handlers.get(task["kind"])
        if handler is None:
            raise RuntimeError(f"Type de tâche inconnu : {task['kind']}")
        self._inner = asyncio.ensure_future(handler(task))
        try:
            await self._inner
        except asyncio.CancelledError:
            if task["id"] not in self._cancelled:
                raise
            self._cancelled.discard(task["id"])
            db.finish_task(task["id"], "cancelled")
            if task["summary_id"]:
                db.update_summary(task["summary_id"], status="cancelled", finished_at=db.now_iso())
            return "cancelled"
        db.finish_task(task["id"], "done")
        return "done"

    async def _cancel_current(self) -> None:
        task = self._current
        if task is None:
            return
        self._cancelled.add(task["id"])
        if self._inner is not None:
            self._inner.cancel()
        elif self._proc is not None and self._proc.returncode is None:
            self._terminate(signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(self._proc.wait()), timeout=TERMINATE_GRACE)
            except TimeoutError:
                self._terminate(signal.SIGKILL)

    def _terminate(self, sig: int) -> None:
        try:
            # Tout le groupe : ffmpeg, yt-dlp… lancés par le pipeline
            os.killpg(self._proc.pid, sig)
        except ProcessLookupError:
            pass

    async def _mark_failed(self, task: dict, message: str) -> None:
        db.finish_task(task["id"], "failed", message)
        if task.get("summary_id"):
            db.update_summary(task["summary_id"], status="failed", error=message, finished_at=db.now_iso())
        elif task["kind"] == "rediarize":
            db.update_job(task["job_id"], status="completed", progress=100,
                          error=f"Nouvelle diarisation en échec : {message}", finished_at=db.now_iso())
        else:
            db.update_job(task["job_id"], status="failed", error=message, finished_at=db.now_iso())
