import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from . import db
from .api import jobs, speakers, system
from .auth import require_token
from .config import get_settings
from .worker.queue import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    worker = Worker()
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
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/api/health", tags=["system"], summary="Sonde de vie (sans authentification)")
    def health():
        return {"status": "ok"}

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
    for module in (jobs, speakers, system):
        api.include_router(module.router)
    app.include_router(api)

    web_dir = get_settings().web_dir
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app


app = create_app()
