from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

WHISPER_MODELS = ("large-v3", "large-v3-turbo")
LANGUAGES = ("fr", "en")
DEVICES = ("cuda", "cpu")

APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuration, lue depuis les variables d'environnement (insensibles à la casse)."""

    data_dir: Path = Path("/data")
    web_dir: Path = APP_ROOT / "web"
    tz: str = "Europe/Paris"
    # Base des liens absolus envoyés dans les callbacks, ex. http://mon-serveur:8090
    public_base_url: str = ""
    api_token: str = ""
    callback_token: str = ""
    max_upload_mb: int = 4096

    # Transcription
    hf_token: str = ""
    default_model: str = "large-v3"
    default_language: str = "fr"
    default_device: str = "cuda"
    batch_size: int = 16
    min_free_vram_gb: float = 10.0
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    # Pipeline factice (tests, sans GPU ni WhisperX)
    fake_pipeline: bool = False
    fake_pipeline_delay: float = 0.0

    # LLM
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3.8:27b"
    ollama_max_ctx: int = 65536
    ollama_unload_timeout: float = 30.0
    llm_api_base_url: str = ""
    llm_api_key: str = ""
    llm_api_model: str = ""
    llm_timeout: float = 1800.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scribe.db"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def cookies_file(self) -> Path:
        return self.config_dir / "youtube-cookies.txt"

    @property
    def llm_api_configured(self) -> bool:
        return bool(self.llm_api_base_url and self.llm_api_model)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id


@lru_cache
def get_settings() -> Settings:
    return Settings()
