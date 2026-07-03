"""
Prediction orchestration + ForeFire integration seam.

This module is the ONLY place that knows how to turn a PredictRequest into
isochrones. It:

  1. Gathers the model inputs (wind from weather.py, fuel from fuel.py, slope
     from terrain.py) unless the caller supplied overrides.
  2. Picks an engine per settings.PREDICTION_ENGINE:
       - "builtin"  -> always the elliptical model in spread_model.py
       - "forefire" -> require ForeFire; error if unavailable
       - "auto"     -> ForeFire if importable/compiled, else builtin
  3. Returns a PredictResponse.

The ForeFire path is deliberately isolated in `_run_forefire`. ForeFire is a C++
engine driven either through its Python bindings (`pyforefire`) or a compiled
`forefire` binary fed a command script + a NetCDF landscape. Building that
landscape (fuel + elevation + wind on a common grid) is the real integration
work — see docs/FOREFIRE_SETUP.md. Until it is wired, `_run_forefire` raises
ForeFireUnavailable and "auto" falls back cleanly.
"""
from typing import Any

from ..config import get_settings
from ..schemas import PredictRequest, PredictResponse
from . import fuel as fuel_svc
from . import spread_model
from . import terrain as terrain_svc
from . import weather as weather_svc


class ForeFireUnavailable(RuntimeError):
    """Raised when the ForeFire engine is requested but not installed/wired."""


def _forefire_available() -> bool:
    settings = get_settings()
    try:
        import pyforefire  # noqa: F401
        return True
    except Exception:
        pass
    return bool(settings.forefire_binary)


async def _gather_inputs(req: PredictRequest) -> dict[str, Any]:
    """Resolve wind, fuel, and slope, using overrides when provided."""
    notes: list[str] = []

    wind_speed = req.wind_speed_kmh
    wind_dir = req.wind_direction_deg
    if wind_speed is None or wind_dir is None:
        wx = await weather_svc.current(req.lat, req.lon)
        if wind_speed is None:
            wind_speed = wx.wind_speed_kmh if wx.wind_speed_kmh is not None else 15.0
        if wind_dir is None:
            wind_dir = wx.wind_direction_deg if wx.wind_direction_deg is not None else 270.0
        notes.append(f"Wind from {wx.source}: {wind_speed:.0f} km/h @ {wind_dir:.0f}deg (from).")

    fuel_code = req.fuel_model or await fuel_svc.fuel_at(req.lat, req.lon)
    fuel_params = fuel_svc.get_params(fuel_code)
    if req.fuel_model is None:
        notes.append(f"Fuel model: {fuel_params['code']} ({fuel_params['name']}).")

    slope = req.slope_percent
    if slope is None:
        slope = await terrain_svc.slope_percent_at(req.lat, req.lon)
        if slope is None:
            slope = 0.0
            notes.append("Slope unavailable; assumed flat (0%).")
        else:
            notes.append(f"Estimated local slope: {slope:.0f}%.")

    return {
        "wind_speed_kmh": float(wind_speed),
        "wind_direction_deg": float(wind_dir),
        "fuel": fuel_params,
        "slope_percent": float(slope),
        "notes": notes,
    }


def _run_forefire(req: PredictRequest, inputs: dict[str, Any]) -> dict[str, Any]:
    """
    Placeholder for the real ForeFire run. Wiring steps (see docs):
      1. Clip a LANDFIRE fuel raster + a 3DEP DEM around (lat,lon).
      2. Write a ForeFire NetCDF landscape + fuels.ff parameter file.
      3. Set the ignition (point or imported perimeter) and wind.
      4. Step the simulation and export fronts as GeoJSON.
    Until implemented, signal unavailability so "auto" can fall back.
    """
    raise ForeFireUnavailable(
        "ForeFire engine not yet wired (landscape NetCDF builder pending). "
        "See docs/FOREFIRE_SETUP.md."
    )


async def predict(req: PredictRequest) -> PredictResponse:
    settings = get_settings()
    inputs = await _gather_inputs(req)
    notes = list(inputs["notes"])

    engine_pref = settings.prediction_engine.lower()
    use_forefire = engine_pref == "forefire" or (engine_pref == "auto" and _forefire_available())

    if use_forefire:
        try:
            result = _run_forefire(req, inputs)
            return PredictResponse(
                engine="forefire",
                parameters=_params(req, inputs),
                isochrones=result,
                notes=notes,
            )
        except ForeFireUnavailable as exc:
            if engine_pref == "forefire":
                raise
            notes.append(f"ForeFire unavailable, used built-in model. ({exc})")

    fc = spread_model.simulate(
        lat=req.lat,
        lon=req.lon,
        duration_hours=req.duration_hours,
        step_minutes=req.step_minutes,
        wind_speed_kmh=inputs["wind_speed_kmh"],
        wind_direction_deg=inputs["wind_direction_deg"],
        ros_ref=inputs["fuel"]["ros_ref"],
        wind_factor=inputs["fuel"]["wind_factor"],
        slope_percent=inputs["slope_percent"],
    )
    # Surface the model-derived quantities in the notes for transparency.
    p = fc.get("properties", {})
    notes.append(
        f"Built-in elliptical model: head ROS {p.get('head_ros_m_per_min')} m/min, "
        f"L:B {p.get('length_to_breadth')}."
    )
    return PredictResponse(
        engine="builtin",
        parameters=_params(req, inputs),
        isochrones=fc,
        notes=notes,
    )


def _params(req: PredictRequest, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin": {"lat": req.lat, "lon": req.lon},
        "duration_hours": req.duration_hours,
        "step_minutes": req.step_minutes,
        "wind_speed_kmh": round(inputs["wind_speed_kmh"], 1),
        "wind_direction_deg": round(inputs["wind_direction_deg"], 1),
        "fuel_model": inputs["fuel"]["code"],
        "fuel_name": inputs["fuel"]["name"],
        "slope_percent": inputs["slope_percent"],
    }
