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

    forefire_binary: str = ""
    # ForeFire propagation model. "Farsite" pairs with the built-in
    # STDfarsiteFuelsTable (keyed by LANDFIRE FBFM40 indices) and needs the
    # moisture params set in forefire_adapter. Other options: "Rothermel",
    # "WindDriven" — but those expect different fuel tables.
    forefire_propagation_model: str = "Farsite"

    # Empirical spread-adjustment factor (multiplies the wind the fire sees).
    # 1.0 = raw model. An earlier 0.5 was calibrated against FIRMS hotspot
    # footprints (which looked over-predicted ~1.5×) — but validating against
    # REAL GeoMAC/NIFC perimeters showed that was a proxy artifact: FIRMS
    # footprints UNDER-represent the true burned area, so the raw model is not
    # systematically over-predicting (it under-predicts active-growth days vs real
    # perimeters and over-predicts quiet days — the normal free-spread variance).
    # So we default to 1.0. See validation/README.md. Per-request `waf_scale`
    # overrides this.
    spread_wind_adjust: float = 1.0

    # Optional geocoder upgrade
    mapbox_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
