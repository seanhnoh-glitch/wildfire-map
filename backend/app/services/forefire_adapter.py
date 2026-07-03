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
from . import fires as fires_svc
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


def _steps(req: PredictRequest) -> int:
    return max(1, int(round(req.duration_hours * 60 / req.step_minutes)))


async def _build_wind_series(req: PredictRequest, notes: list[str]) -> tuple[list[tuple[float, float]], str]:
    """
    Produce one (speed_kmh, dir_from_deg) per forecast step.

      - explicit override  -> constant wind, repeated for every step
      - use_forecast_wind  -> HRRR-backed hourly forecast, sampled per step
      - otherwise / on error -> constant current wind

    Returns (series, wind_source_label).
    """
    n = _steps(req)

    if req.wind_speed_kmh is not None and req.wind_direction_deg is not None:
        notes.append(
            f"Wind held constant at {req.wind_speed_kmh:.0f} km/h @ {req.wind_direction_deg:.0f}deg (override)."
        )
        return [(req.wind_speed_kmh, req.wind_direction_deg)] * n, "override (constant)"

    if req.use_forecast_wind:
        try:
            hourly = await weather_svc.forecast_hourly(req.lat, req.lon, int(req.duration_hours) + 1)
            series: list[tuple[float, float]] = []
            for k in range(n):
                # Map step k (ending at minute (k+1)*step) to the hour it falls in.
                hour_idx = min(len(hourly) - 1, int((k * req.step_minutes) // 60))
                h = hourly[hour_idx]
                series.append((float(h["wind_speed_kmh"]), float(h["wind_direction_deg"])))
            first, last = series[0], series[-1]
            notes.append(
                f"HRRR-backed forecast wind: start {first[0]:.0f} km/h @ {first[1]:.0f}deg -> "
                f"end {last[0]:.0f} km/h @ {last[1]:.0f}deg."
            )
            return series, "Open-Meteo hourly (HRRR-backed)"
        except Exception as exc:
            notes.append(f"Forecast wind unavailable ({exc}); using constant current wind.")

    wx = await weather_svc.current(req.lat, req.lon)
    speed = wx.wind_speed_kmh if wx.wind_speed_kmh is not None else 15.0
    direction = wx.wind_direction_deg if wx.wind_direction_deg is not None else 270.0
    notes.append(f"Current wind from {wx.source}: {speed:.0f} km/h @ {direction:.0f}deg (held constant).")
    return [(speed, direction)] * n, wx.source


async def _gather_inputs(req: PredictRequest) -> dict[str, Any]:
    """Resolve the wind series, fuel, and slope, using overrides when provided."""
    notes: list[str] = []

    wind_series, wind_source = await _build_wind_series(req, notes)

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

    # Ignition: start from the real mapped fire footprint when one exists, so a
    # large fire's forecast grows from its actual perimeter rather than a point.
    origin_lat, origin_lon = req.lat, req.lon
    initial_front = None
    ignition = "point"
    if req.ignite_from_perimeter:
        try:
            geom = await fires_svc.nearest_perimeter_geometry(req.lat, req.lon, radius_km=8.0)
            parsed = spread_model.perimeter_to_front(geom) if geom else None
            if parsed:
                origin_lat, origin_lon, initial_front = parsed
                ignition = "perimeter"
                notes.append("Ignited from mapped NIFC perimeter (fire's current footprint).")
            else:
                notes.append("No usable perimeter nearby; ignited from the point.")
        except Exception as exc:
            notes.append(f"Perimeter lookup failed ({exc}); ignited from the point.")

    return {
        "wind_series": wind_series,
        "wind_source": wind_source,
        "fuel": fuel_params,
        "slope_percent": float(slope),
        "origin_lat": origin_lat,
        "origin_lon": origin_lon,
        "initial_front": initial_front,
        "ignition": ignition,
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

    fc = spread_model.simulate_timevarying(
        lat=inputs["origin_lat"],
        lon=inputs["origin_lon"],
        wind_series=inputs["wind_series"],
        ros_ref=inputs["fuel"]["ros_ref"],
        wind_factor=inputs["fuel"]["wind_factor"],
        slope_percent=inputs["slope_percent"],
        step_minutes=req.step_minutes,
        initial_front=inputs["initial_front"],
    )
    notes.append(
        f"Built-in time-varying elliptical model ({fc['properties']['steps']} steps, "
        f"ignition: {inputs['ignition']}, wind: {inputs['wind_source']})."
    )
    return PredictResponse(
        engine="builtin",
        parameters=_params(req, inputs),
        isochrones=fc,
        notes=notes,
    )


def _params(req: PredictRequest, inputs: dict[str, Any]) -> dict[str, Any]:
    series = inputs["wind_series"]
    start_speed, start_dir = series[0]
    end_speed, end_dir = series[-1]
    return {
        "origin": {"lat": req.lat, "lon": req.lon},
        "ignition": inputs["ignition"],
        "duration_hours": req.duration_hours,
        "step_minutes": req.step_minutes,
        "wind_source": inputs["wind_source"],
        "wind_speed_kmh": round(start_speed, 1),          # start value (map label)
        "wind_direction_deg": round(start_dir, 1),
        "wind_end_speed_kmh": round(end_speed, 1),
        "wind_end_direction_deg": round(end_dir, 1),
        "fuel_model": inputs["fuel"]["code"],
        "fuel_name": inputs["fuel"]["name"],
        "slope_percent": inputs["slope_percent"],
    }
