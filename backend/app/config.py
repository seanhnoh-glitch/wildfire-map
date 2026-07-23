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

    # ForeFire resolution / fidelity. Defaults are tuned for a ~150 s web request
    # (coarse enough that big fires finish); validation has no such budget, so set
    # these higher to run ForeFire nearer its real capability (finer front + layers
    # + fuel/DEM grids) and measure whether the coarsening is costing accuracy.
    #   grid_n:            layer grid is grid_n × grid_n over the domain
    #   perim_res:         front node spacing = clamp(fire_half / div, min, max) metres
    #   time_budget_s:     per-simulation wall-clock cap (returns partial past it)
    #   fuel_grid_samples: LANDFIRE getSamples count (≈ sqrt × sqrt cells)
    #   elev_grid_n:       DEM grid is elev_grid_n × elev_grid_n
    forefire_grid_n: int = 100
    forefire_perim_res_div: float = 90.0
    # Front-node spacing floor (m). Small fires sit at this floor, so it sets how much
    # perimeter detail a small-fire forecast keeps. The per-step cost is bounded by the
    # DIVISOR (max nodes ≈ 2π·div ≈ 565, at fire_half = div·min), NOT the floor, so a
    # finer floor sharpens small fires without raising the worst-case node count. 60 m
    # keeps a few-hundred-acre fire's shape (was 200 m, which rounded it to ~20 nodes).
    forefire_perim_res_min: float = 60.0
    forefire_perim_res_max: float = 2500.0
    forefire_time_budget_s: float = 150.0
    fuel_grid_samples: int = 900
    elev_grid_n: int = 28

    # Feed ForeFire a real elevation-raster (Copernicus DEM) grid so it derives
    # local slope/aspect per node, instead of a single domain-wide tilted plane
    # from a point-sampled slope. This is the more physically correct terrain input,
    # but against real GeoMAC perimeters it did NOT improve overlap accuracy (the
    # crude tilted plane's uniform upslope push happens to offset the model's
    # under-prediction; the true gap is crown/spotting, which slope can't fix). So
    # it's OFF by default and opt-in via TERRAIN_DEM=true. See validation/README.md.
    terrain_dem: bool = False

    # Gust-blended driving wind: the wind the fire spreads on is
    #   effective = sustained + wind_gust_factor · (gust − sustained).
    # Hourly-mean 10 m wind badly under-represents the gusts that carry fire runs —
    # the single biggest reason the forecast under-predicted. Validating against real
    # GeoMAC perimeters, 0.5 (paired with crown_spotting) centred the area bias at
    # ~1.0 and improved skill. 0.0 = sustained only (old behaviour).
    wind_gust_factor: float = 0.5

    # Regime-scaled aggressiveness: damp the model on calm/humid days and keep it
    # full on hot-dry-windy run days, using a Hot-Dry-Windy fire-weather index (see
    # _regime_factor). Fixes the over-prediction on normal/quiet days that a fixed
    # gust+spotting default causes, without losing the run-day calibration.
    #   regime_wind_min: driving-wind multiplier at the calm extreme (regime=0)
    #   regime_wind_max: driving-wind multiplier at the hot-dry-windy extreme (regime=1);
    #     >1 boosts the most extreme run days (which the surface model under-predicts).
    #   regime_spot_boost: extra spotting reach at regime=1 (lateral area added where a
    #     pure wind boost would just overshoot downwind).
    regime_scaling: bool = True
    regime_wind_min: float = 0.6
    regime_wind_max: float = 1.0
    regime_spot_boost: float = 1.0

    # Suppression damping: a free-spread model over-predicts fires that crews/lines
    # are holding (it can't see suppression). We damp the driving wind by a
    # suppression signal in [0,1] — from reported containment (production) or recent
    # growth momentum (validation). suppression_damp is the wind reduction at the
    # fully-suppressed extreme (1.0); containment_to_suppression maps % contained →
    # the signal (lined edges suppress, but the head may still run, so < 1).
    suppression_scaling: bool = True
    suppression_damp: float = 0.6
    containment_to_suppression: float = 0.7

    # ML residual correction (Phase 5): rescale the forecast footprint by a learned
    # observed/forecast factor (services/ml_correction.py, trained by
    # validation/train_correction.py). Validated (validation/phase4_skill.py) to lift
    # held-out Jaccard +0.05 (+0.10 on quiet days) with no harm to run days, and a
    # feature-parity check (validation/parity_check.py) confirms the live features
    # match training. ON by default; a no-op if the model file / scikit-learn are
    # absent, so the engine still runs without them. Set ML_CORRECTION=false to disable.
    ml_correction: bool = True

    # Add crown-fire ember spotting on top of ForeFire's surface footprint (see
    # services/spotting.py) — the missing mechanism behind under-prediction on
    # plume-driven timber run days. On by default: validation showed gusts alone
    # overshoot downwind, but gusts + spotting together centre the area bias and
    # improve skill (spotting spreads the extra growth into a lateral fan). Set
    # CROWN_SPOTTING=false to disable.
    crown_spotting: bool = True

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

    # Mapbox access token. Powers the traffic-aware evacuation routing
    # (Directions `driving-traffic` profile) and can also upgrade the geocoder.
    # Without it, /evacuation still returns drive routes via the keyless OSRM server
    # (no live traffic) plus the safe destinations.
    mapbox_token: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
