import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__, callbacks, db
from .api import jobs, prompts, search, settings, speakers, summaries, system
from .auth import require_token
from .config import get_settings
from .llm.summarize import run_summary_task
from .worker.queue import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    worker = Worker()
    worker.handlers["summarize"] = run_summary_task
    worker.on_task_done.append(callbacks.on_task_done)
    app.state.worker = worker
    loop_task = asyncio.create_task(worker.run_forever())
    try:
        yield
    finally:
        await worker.shutdown()
        loop_task.cancel()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Meeting Scribe",
        description="Transcription de réunions (WhisperX + pyannote), identification des intervenants, "
                    "transcripts Markdown et comptes rendus par LLM.",
        version=__version__,
        lifespan=lifespan,
    )

    @app.get("/api/health", tags=["system"], summary="Sonde de vie (sans authentification)")
    def health():
        return {"status": "ok"}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    for module in (jobs, speakers, summaries, prompts, settings, search, system):
        api.include_router(module.router)
    app.include_router(api)

    @app.middleware("http")
    async def revalidate_web(request, call_next):
        # Interface : le navigateur revalide chaque fichier (ETag, réponse 304 s'il n'a pas changé). Sans cela,
        # après une mise à jour, il peut garder l'ancien app.js avec le nouveau HTML et la page reste vide.
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    web_dir = get_settings().web_dir
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
