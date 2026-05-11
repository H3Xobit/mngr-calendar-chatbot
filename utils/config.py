"""Application configuration via environment variables.

Loaded once at startup. Importing this module also calls ``load_dotenv()``
so ``.env`` works in local development without any other wiring.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False, extra="ignore")

    # Groq
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    groq_model: str = Field(default="llama-3.3-70b-versatile", alias="GROQ_MODEL")

    # Google OAuth2
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(
        default="http://localhost:8000/auth/google/callback",
        alias="GOOGLE_REDIRECT_URI",
    )

    # App
    session_secret: str = Field(
        default="dev-only-please-change", alias="SESSION_SECRET"
    )
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # Storage paths
    token_dir: Path = PROJECT_ROOT / "tokens"
    log_dir: Path = PROJECT_ROOT / "logs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.token_dir.mkdir(parents=True, exist_ok=True)
    s.log_dir.mkdir(parents=True, exist_ok=True)
    # Allow OAuth on http://localhost during local development.
    if os.getenv("OAUTHLIB_INSECURE_TRANSPORT") == "1":
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    return s
