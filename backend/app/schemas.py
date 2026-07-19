"""
Pydantic request/response schemas — the contract the mobile app codes against.

GeoJSON is represented loosely as dict[str, Any] because it is passed straight
through to the map; the typed models here are for the app-specific payloads.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


# --- Geocoding ---------------------------------------------------------------

class GeocodeResult(BaseModel):
    lat: float
    lon: float
    label: str = Field(description="Human-readable matched address")


# --- Fires -------------------------------------------------------------------

class Fire(BaseModel):
    """A single active wildfire incident (US NIFC WFIGS or Canadian CWFIS)."""
    id: str
    name: str
    lat: float
    lon: float
    distance_km: float
    size_acres: Optional[float] = None
    percent_contained: Optional[float] = None
    discovery_time: Optional[str] = None
    county: Optional[str] = None
    state: Optional[str] = None
    # Which country the incident is in ("US" or "CA"), so the client can label the
    # source and pick the right containment semantics.
    country: Optional[str] = None
    # Canada reports a categorical "stage of control" (Out of Control / Being Held /
    # Under Control) rather than a containment percentage, so percent_contained is
    # None for Canadian fires and this carries the status instead.
    stage_of_control: Optional[str] = None


class NearbyFiresResponse(BaseModel):
    query: dict[str, float]
    radius_km: float
    count: int
    fires: list[Fire]
    # Satellite hotspot detections (NASA FIRMS) as a GeoJSON FeatureCollection,
    # or None if no FIRMS key is configured.
    hotspots: Optional[dict[str, Any]] = None
    # Official mapped perimeters (NIFC) as a GeoJSON FeatureCollection.
    perimeters: Optional[dict[str, Any]] = None


# --- Weather -----------------------------------------------------------------

class WeatherConditions(BaseModel):
    source: str
    time: Optional[str] = None
    temperature_c: Optional[float] = None
    relative_humidity: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = Field(
        default=None, description="Meteorological direction the wind blows FROM, degrees"
    )
    wind_gust_kmh: Optional[float] = None


# --- Prediction --------------------------------------------------------------

class PredictRequest(BaseModel):
    lat: float = Field(description="Ignition / current fire latitude")
    lon: float = Field(description="Ignition / current fire longitude")
    duration_hours: float = Field(default=6.0, ge=0.5, le=48.0)
    step_minutes: int = Field(default=60, ge=15, le=360)
    # Optional overrides; if omitted the service fetches live weather at the point.
    # When set, wind is held CONSTANT at these values for the whole forecast.
    wind_speed_kmh: Optional[float] = None
    wind_direction_deg: Optional[float] = None
    # Use hourly HRRR-backed forecast wind so the fire bends as the wind shifts.
    # Ignored if an explicit wind override above is supplied.
    use_forecast_wind: bool = True
    # Optional fuel/terrain hints; if omitted the service uses live/default layers.
    fuel_model: Optional[str] = Field(default=None, description="Scott & Burgan code, e.g. 'GR2', 'TU5'")
    slope_percent: Optional[float] = None
    # If an official perimeter is available, ignite from it instead of a point.
    ignite_from_perimeter: bool = True
    # Reported containment (0-100%) of the fire being forecast, if known. At 100%
    # the fire has a complete control line and won't spread, so the free-spread
    # forecast is skipped; below 100% it's used to hedge the forecast as a worst
    # case. Supplied by the client (the map knows it from the fire record).
    percent_contained: Optional[float] = None

    # --- Hindcast / retrospective overrides (used by validation/retrospective) ---
    # Ignite from this explicit GeoJSON geometry instead of the nearest live
    # perimeter (so a past fire's T0 footprint can be replayed).
    ignition_geojson: Optional[dict] = None
    # Explicit per-step wind [[speed_kmh, dir_from_deg], ...] instead of live
    # current/forecast wind (so a past window's real wind can be replayed).
    wind_series: Optional[list[list[float]]] = None
    # Explicit temperature (°C) + relative humidity (%) for fuel moisture instead
    # of live weather.
    temperature_c: Optional[float] = None
    relative_humidity: Optional[float] = None
    # Multiply the per-fuel wind adjustment factor (10 m → midflame). 1.0 = default;
    # >1 lets more wind through (faster spread). For the WAF sensitivity experiment.
    waf_scale: Optional[float] = None


class PredictResponse(BaseModel):
    engine: str = Field(description="Which engine produced this: 'forefire' or 'builtin'")
    model_config = {"protected_namespaces": ()}
    parameters: dict[str, Any]
    # Time-stamped fire fronts as a GeoJSON FeatureCollection (one polygon per step).
    isochrones: dict[str, Any]
    notes: list[str] = []
    # True when no spread forecast was produced because the fire is fully contained
    # (100%). The isochrones are then empty and the client should say so.
    contained: bool = False


# --- Evacuation routing ------------------------------------------------------

class EvacuationRequest(BaseModel):
    lat: float = Field(description="The user's current latitude")
    lon: float = Field(description="The user's current longitude")
    # Fire reference point, for the "route away from the fire" direction. If omitted
    # we use the centroid of the danger area.
    fire_lat: Optional[float] = None
    fire_lon: Optional[float] = None
    # Area to avoid: the fire's current perimeter plus, ideally, the forecast spread
    # (pass the /predict `isochrones`). GeoJSON geometry / Feature / FeatureCollection.
    # If omitted the nearest live perimeter is fetched and avoided.
    avoid_geojson: Optional[dict] = None
    max_routes: int = Field(default=3, ge=1, le=6)


class EvacuationRoute(BaseModel):
    destination: dict[str, Any]
    geometry: dict[str, Any] = Field(description="GeoJSON LineString of the drive")
    distance_km: float
    duration_min: float = Field(description="Drive time WITH current traffic")
    duration_typical_min: Optional[float] = Field(
        default=None, description="Typical drive time without live traffic (delay indicator)"
    )
    passes_near_fire: bool
    recommended: bool = False
    km_from_fire: Optional[float] = None


class EvacuationResponse(BaseModel):
    origin: dict[str, float]
    away_bearing: float = Field(description="Compass bearing pointing away from the fire")
    routes: list[EvacuationRoute]
    destinations: list[dict[str, Any]] = Field(
        default_factory=list, description="Safe destinations considered (points)"
    )
    notes: list[str] = []
