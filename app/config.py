from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).resolve().parents[2]  # Project root

# Debug: Log the BASE_DIR path
print(f"DEBUG: BASE_DIR = {BASE_DIR}")
# .env
# Fallback: Try to find .env in parent directories
ENV_FILE = BASE_DIR / ".env"
print(ENV_FILE)
if not ENV_FILE.exists():
    # Try current working directory
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        ENV_FILE = cwd_env
        print(f"DEBUG: Using .env from CWD: {ENV_FILE}")
    else:
        print(f"DEBUG: .env not found in BASE_DIR or CWD")

# file_transfer\app\.env
class Settings(BaseSettings):
    uploads_path: str = "../uploads"  # Relative to backend directory
    pairing_code_ttl: int = 3600  # 1 hour in seconds
    max_connections: int = 50
    frontend_url: str | None = None
    mongo_url: str | None = None
    mongo_db_name: str = "nexdrop"
    admin_owner_username: str = "owner"
    # admin_admin_username: str = "admin"
    # admin_operator_username: str = "operator"
    # admin_viewer_username: str = "viewer"
    admin_token_secret: str = "nexdrop-admin-dev-secret"
    admin_token_ttl_seconds: int = 60 * 60 * 24 * 7
    admin_owner_password: str = "owner_special_pass"
    admin_owner_email: str = "nexdrop.team@gmail.com"
    # admin_admin_password: str = "admin-pass"
    # admin_operator_password: str = "operator-pass"
    # admin_viewer_password: str = "viewer-pass"
    
    # Mail relay service
    mail_relay_url: str = "https://mail-services-silk.vercel.app/"

    model_config = {
        "env_file": ENV_FILE,
        "env_file_encoding": "utf-8",
    }

    def model_post_init(self, __context) -> None:
        env_file = BASE_DIR / ".env"
        print(f"Looking for .env at: {env_file}")
        print(f".env exists: {env_file.exists()}")
        print(f"Loaded mail relay URL: {self.mail_relay_url}")


def get_settings() -> Settings:
    settings = Settings()
    # Resolve relative paths
    if settings.uploads_path.startswith("../") or settings.uploads_path.startswith("..\\"):
        settings.uploads_path = str((BASE_DIR / settings.uploads_path).resolve())   
    print(f"Settings loaded: uploads_path = {settings.uploads_path}")
    print(f"Mail relay URL: {settings.mail_relay_url}")
    return settings
