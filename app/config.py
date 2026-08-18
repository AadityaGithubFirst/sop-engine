"""Application configuration.

Every setting has a safe local default so the engine boots on an air-gapped
machine with no .env file and no network access beyond the loopback interface.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# The directory holding `.env`, `requirements.txt` and the launcher scripts:
# app/config.py -> app/ -> project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _default_output_dir() -> str:
    """`/tmp/sop_engine` on POSIX, the equivalent temp path on Windows."""
    return str(Path(tempfile.gettempdir()) / "sop_engine")


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or `.env`."""

    # `.env` is resolved against the project root, not the working directory:
    # the launcher and the container both start the server from elsewhere, and
    # a relative path would silently fall back to defaults with no warning.
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),  # allow the MODEL_NAME field name
    )

    # --- Local inference runtime -------------------------------------------
    OLLAMA_HOST: str = "http://127.0.0.1:11434"
    MODEL_NAME: str = "deepseek-r1:8b"
    FAST_MODEL: str = "llama3.1:8b"

    # --- Generation controls ------------------------------------------------
    TEMPERATURE: float = 0.2
    NUM_CTX: int = 8192
    REQUEST_TIMEOUT: int = 600

    # --- Interactive lookups ------------------------------------------------
    # Tool research runs while the user waits, so it gets its own short budget
    # rather than inheriting REQUEST_TIMEOUT (which sizes whole documents).
    RESEARCH_TIMEOUT: int = 45
    RESEARCH_NUM_PREDICT: int = 220

    # --- Multi-pass validation ----------------------------------------------
    # Retries per pass when the validation gate rejects the output. After the
    # budget is spent, deterministic repair fills the gaps so a valid document
    # is always produced.
    MAX_VALIDATION_RETRIES: int = 2
    # When true, a document that still fails the gate after deterministic
    # repair is rejected instead of released.
    STRICT_VALIDATION: bool = True

    # --- Artifact storage ---------------------------------------------------
    OUTPUT_DIR: str = _default_output_dir()
    PUBLIC_BASE_URL: str = "http://127.0.0.1:8000"
    # Writable store for catalog additions and the pending-approval queue.
    # Kept out of the install directory so a reinstall never destroys it.
    DATA_DIR: str = str(Path.home() / ".sop-engine")

    # --- Catalog and tool research ------------------------------------------
    # OFF by default: the product promise is air-gapped execution. Turning this
    # on lets the tool-lookup helper query public reference sites, which sends
    # the tool NAME (never project data) off the machine. It must be an
    # explicit, logged decision by the operator.
    ALLOW_WEB_LOOKUP: bool = False
    # Shared secret for the administrator screen. Change it before any
    # multi-user deployment; on a personal machine the default is adequate
    # because the server binds to localhost only.
    ADMIN_TOKEN: str = "change-me"

    # --- Metadata -----------------------------------------------------------
    APP_NAME: str = "Autonomous SOP Generation Engine"
    APP_VERSION: str = "1.0.0"

    @property
    def data_path(self) -> Path:
        """Writable data directory, created on first access."""
        path = Path(self.DATA_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def output_path(self) -> Path:
        """Output directory, created on first access."""
        path = Path(self.OUTPUT_DIR)
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton (safe to call from FastAPI dependencies)."""
    return Settings()


settings = get_settings()
