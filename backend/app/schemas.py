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
    """A single active wildfire incident (from NIFC WFIGS)."""
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


class PredictResponse(BaseModel):
    engine: str = Field(description="Which engine produced this: 'forefire' or 'builtin'")
    model_config = {"protected_namespaces": ()}
    parameters: dict[str, Any]
    # Time-stamped fire fronts as a GeoJSON FeatureCollection (one polygon per step).
    isochrones: dict[str, Any]
    notes: list[str] = []
