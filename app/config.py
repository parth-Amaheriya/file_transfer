from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    uploads_path: str = str(BASE_DIR / "uploads")
    pairing_code_ttl: int = 3600  # 1 hour in seconds
    max_connections: int = 100

    model_config = {
        "env_file": BASE_DIR / ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
