from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[2]  # Project root


class Settings(BaseSettings):
    uploads_path: str = "../uploads"  # Relative to backend directory
    pairing_code_ttl: int = 3600  # 1 hour in seconds
    max_connections: int = 50
    frontend_url: str | None = None
    mongo_url: str | None = None
    mongo_db_name: str = "nexdrop"
    admin_owner_username: str = "owner"
    admin_admin_username: str = "admin"
    admin_operator_username: str = "operator"
    admin_viewer_username: str = "viewer"
    admin_token_secret: str = "nexdrop-admin-dev-secret"
    admin_token_ttl_seconds: int = 60 * 60 * 24 * 7
    admin_owner_password: str = "owner-pass"
    admin_admin_password: str = "admin-pass"
    admin_operator_password: str = "operator-pass"
    admin_viewer_password: str = "viewer-pass"

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
