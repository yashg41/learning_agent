"""Dropzone settings. Mirrors the pattern in backend/config.py."""

import os
import secrets
from pydantic_settings import BaseSettings

_APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_APP_ROOT, ".env")


class Settings(BaseSettings):
    # Bind loopback by default: cloudflared connects locally, so nothing needs
    # to listen on a routable interface. DROPZONE_HOST=0.0.0.0 is the explicit
    # LAN escape hatch (clipboard API will be unavailable over plain http).
    DROPZONE_HOST: str = "127.0.0.1"
    DROPZONE_PORT: int = 8002

    # Buffer bounds. This is a hand-off buffer, not an archive: only the few
    # most recent items are useful, and holding more just keeps pasted content
    # (and its memory) around longer than needed.
    MAX_ITEMS: int = 3
    MAX_ITEM_BYTES: int = 25 * 1024 * 1024     # per-item; over this -> 413
    MAX_TOTAL_BYTES: int = 75 * 1024 * 1024    # across buffer; evicts oldest
    ITEM_TTL_SECONDS: int = 2 * 60 * 60

    # Regenerated every start unless pinned, so a stale QR stops working.
    DROPZONE_TOKEN: str = ""

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()

if not settings.DROPZONE_TOKEN:
    settings.DROPZONE_TOKEN = secrets.token_urlsafe(16)
