import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

WHISPER_MODELS = ("large-v3", "large-v3-turbo")
LANGUAGES = ("fr", "en")
DEVICES = ("cuda", "cpu")
# VRAM libre exigée avant de lancer un job GPU (Go) : pic mesuré pendant la diarisation, avec une marge
MIN_VRAM_GB = {"large-v3": 10.0, "large-v3-turbo": 6.0}

APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Configuration, lue depuis les variables d'environnement (insensibles à la casse)."""

    # Une variable vide dans .env vaut valeur par défaut
    model_config = SettingsConfigDict(env_ignore_empty=True)

    data_dir: Path = Path("/data")
    web_dir: Path = APP_ROOT / "web"
    tz: str = "Europe/Paris"
    # Base des liens absolus envoyés dans les callbacks, ex. http://mon-serveur:8090
    public_base_url: str = ""
    api_token: str = ""
    callback_token: str = ""
    max_upload_mb: int = 4096
    # Nom proposé dans l'interface pour nommer un intervenant (le propriétaire de l'instance)
    owner_name: str = ""

    # Transcription
    hf_token: str = ""
    default_model: str = "large-v3"
    default_language: str = "fr"
    default_device: str = "cuda"
    batch_size: int = 16
    # Seuil de VRAM libre imposé quel que soit le modèle (Go) ; vide = selon le modèle (MIN_VRAM_GB)
    min_free_vram_gb: float | None = None
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    # Copie locale du modèle de diarisation (embarquée dans l'image) : ni jeton ni réseau
    diarization_model_dir: Path = Path("/opt/models/pyannote/speaker-diarization-community-1")
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
    def diarization_bundled(self) -> bool:
        return (self.diarization_model_dir / "config.yaml").is_file()

    @property
    def diarization_source(self) -> str:
        """Dossier local s'il est présent, sinon identifiant Hugging Face (exige HF_TOKEN)."""
        return str(self.diarization_model_dir) if self.diarization_bundled else self.diarization_model

    @property
    def diarization_ready(self) -> bool:
        return self.diarization_bundled or bool(self.hf_token)

    def min_vram_gb(self, model: str) -> float:
        if self.min_free_vram_gb is not None:
            return self.min_free_vram_gb
        return MIN_VRAM_GB.get(model, max(MIN_VRAM_GB.values()))

    @property
    def llm_api_configured(self) -> bool:
        return bool(self.llm_api_base_url and self.llm_api_model)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def gpu_available() -> bool:
    """GPU NVIDIA utilisable dans le conteneur (torch CUDA, ou à défaut nvidia-smi)."""
    try:
        import torch
    except ImportError:
        return shutil.which("nvidia-smi") is not None
    return torch.cuda.is_available()


def available_devices(settings: Settings) -> tuple[str, ...]:
    if settings.fake_pipeline or gpu_available():
        return DEVICES
    return ("cpu",)
