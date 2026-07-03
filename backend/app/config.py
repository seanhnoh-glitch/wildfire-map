"""
Application configuration, loaded from environment / a local .env file.

Everything has a sane default so the API boots with zero configuration; the only
thing you *need* for full functionality is a free NASA FIRMS key (see .env.example).
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # NASA FIRMS active-fire detections (optional; NIFC works without it)
    firms_map_key: str = ""

    # Server / CORS
    cors_origins: str = "*"
    host: str = "0.0.0.0"
    port: int = 8000

    # Prediction engine selection: "auto" | "forefire" | "builtin"
    prediction_engine: str = "auto"
    forefire_binary: str = ""

    # Optional geocoder upgrade
    mapbox_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
