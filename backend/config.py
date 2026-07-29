import os
from pydantic_settings import BaseSettings

_APP_ROOT = os.path.dirname(os.path.dirname(__file__))
_ENV_FILE = os.path.join(_APP_ROOT, ".env")


class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str = ""
    MODEL_NAME: str = "claude-haiku-4-5-20251001"

    DATA_DIR: str = os.path.join(_APP_ROOT, "data")
    CHROMA_DIR: str = os.path.join(_APP_ROOT, "data", "chroma")

    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8001
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = _ENV_FILE
        env_file_encoding = "utf-8"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
