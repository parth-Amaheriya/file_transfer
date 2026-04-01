from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[2]  # Project root


class Settings(BaseSettings):
    uploads_path: str = "../uploads"  # Relative to backend directory
    pairing_code_ttl: int = 3600  # 1 hour in seconds
    max_connections: int = 100

    model_config = {
        "env_file": BASE_DIR / ".env",
        "env_file_encoding": "utf-8",
    }


def get_settings() -> Settings:
    settings = Settings()
    # Resolve relative paths
    if settings.uploads_path.startswith("../") or settings.uploads_path.startswith("..\\"):
        settings.uploads_path = str((BASE_DIR / settings.uploads_path).resolve())
    print(f"Settings loaded: uploads_path = {settings.uploads_path}")
    return settings
