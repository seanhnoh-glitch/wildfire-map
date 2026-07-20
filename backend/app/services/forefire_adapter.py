"""
ForeFire front-tracking fire spread engine.

Turns a PredictRequest into isochrones using pyforefire (Python bindings for
the ForeFire C++ simulator). The engine is required — there is no fallback.
If pyforefire is not installed the /predict endpoint returns HTTP 503.

Build requirements: ForeFire must be compiled from source. Use the Docker image
(backend/Dockerfile) or follow docs/FOREFIRE_SETUP.md for WSL/Linux.
"""
import asyncio
import datetime
import logging
import math
import multiprocessing
import re
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from ..config import get_settings
from ..schemas import PredictRequest, PredictResponse
from . import fires as fires_svc
from . import fuel as fuel_svc
from . import ml_correction
from . import spotting
from . import spread_model
from . import terrain as terrain_svc
from . import weather as weather_svc
from .fuel_table import FARSITE_FUEL_TABLE
from .geo import haversine_km, local_meters_to_lonlat, lonlat_to_local_meters


def _domain_extents(initial_polygon) -> tuple[float, float]:
    """
    (domain_half, fire_half) in metres for the ForeFire domain, sized to contain
    the fire perimeter plus spread room. Shared by _gather_inputs (to fetch the
    fuel grid over the right bbox) and _run_forefire (to build the domain), so
    both agree on the extent.
    """
    fire_half = 2_000.0
    half = 40_000.0
    if initial_polygon is not None:
        minx, miny, maxx, maxy = initial_polygon.bounds
        fire_half = max(abs(minx), abs(miny), abs(maxx), abs(maxy))
        half = max(half, fire_half + 20_000.0)
    return min(half, 250_000.0), fire_half

log = logging.getLogger("forefire")

# ForeFire's C++ engine keeps process-global state (domain, fire, parameter
# singletons), so it is NOT safe to run two simulations in one process — the
# second would inherit the first fire's domain and return the wrong result.
# Each prediction therefore runs in its own freshly *spawned* subprocess (see
# predict()), giving the engine a clean global state every time.
_MP_SPAWN = multiprocessing.get_context("spawn")

# Wall-clock budget (seconds) for a single ForeFire simulation. If a step would
# push past this, we stop and return the isochrones computed so far rather than
# letting a huge fire hang the request. Generous enough that a normal 6-step
# forecast completes; it's a runaway guard, not a normal exit path.
_FF_TIME_BUDGET_S = 150.0

# FBFM40 code → fuel index in ForeFire's built-in STDfarsiteFuelsTable.
# That table (SimulationParameters.cpp) is keyed by exactly the LANDFIRE FBFM40
# encoding, so every Scott & Burgan 40 model maps 1:1 to a fully-parameterised
# fuel entry (h1/h10/h100 loads, SAV, depth, moisture of extinction, heat).
_FBFM40_TO_INT: dict[str, int] = {
    # Grass (GR)
    "GR1": 101, "GR2": 102, "GR3": 103, "GR4": 104, "GR5": 105,
    "GR6": 106, "GR7": 107, "GR8": 108, "GR9": 109,
    # Grass-shrub (GS)
    "GS1": 121, "GS2": 122, "GS3": 123, "GS4": 124,
    # Shrub (SH)
    "SH1": 141, "SH2": 142, "SH3": 143, "SH4": 144, "SH5": 145,
    "SH6": 146, "SH7": 147, "SH8": 148, "SH9": 149,
    # Timber-understory (TU)
    "TU1": 161, "TU2": 162, "TU3": 163, "TU4": 164, "TU5": 165,
    # Timber litter (TL)
    "TL1": 181, "TL2": 182, "TL3": 183, "TL4": 184, "TL5": 185,
    "TL6": 186, "TL7": 187, "TL8": 188, "TL9": 189,
    # Slash-blowdown (SB)
    "SB1": 201, "SB2": 202, "SB3": 203, "SB4": 204,
    # Non-burnable → fall back to GR2 so an active-fire forecast still runs
    # (a fire that is actively spreading is not sitting in true non-burnable fuel;
    #  an NB point sample is almost always a raster artefact).
    "NB1": 102, "NB2": 102, "NB3": 102, "NB8": 102, "NB9": 102,
}

# Default fuel index when a code is unknown — GR2, present in STDfarsiteFuelsTable.
_DEFAULT_FUEL_INT = 102

# Fuel bed depth (feet) per FBFM40 code, from ForeFire's STDfarsiteFuelsTable
# ("depth" column). Used to derive the open→midflame wind adjustment factor.
_FUEL_BED_DEPTH_FT: dict[str, float] = {
    "GR1": 0.4, "GR2": 1.0, "GR3": 2.0, "GR4": 2.0, "GR5": 1.5,
    "GR6": 1.5, "GR7": 3.0, "GR8": 4.0, "GR9": 5.0,
    "GS1": 0.9, "GS2": 1.5, "GS3": 1.8, "GS4": 2.1,
    "SH1": 1.0, "SH2": 1.0, "SH3": 2.4, "SH4": 3.0, "SH5": 6.0,
    "SH6": 2.0, "SH7": 6.0, "SH8": 3.0, "SH9": 4.4,
    "TU1": 0.6, "TU2": 1.0, "TU3": 1.3, "TU4": 0.5, "TU5": 1.0,
    "TL1": 0.2, "TL2": 0.2, "TL3": 0.3, "TL4": 0.4, "TL5": 0.6,
    "TL6": 0.3, "TL7": 0.4, "TL8": 0.3, "TL9": 0.6,
    "SB1": 1.0, "SB2": 1.0, "SB3": 1.2, "SB4": 2.7,
}


def _wind_adjustment_factor(fuel_code: str) -> float:
    """
    Open→midflame wind adjustment factor (WAF) for unsheltered fuel, from Andrews
    (2012), as a function of fuel bed depth d (ft):

        WAF = 1.83 / ln((20 + 0.36 d) / (0.13 d))

    Weather gives us 10 m open wind; fire spreads with the *midflame* wind, which
    is slower. We multiply the forecast wind by this factor before feeding the
    model, so wind-driven spread isn't overstated. (10 m ≈ the 20-ft reference
    wind to within ~10%, well inside the model's uncertainty.) Clamped to a sane
    range; ~0.3 for grass/litter up to ~0.55 for tall shrub.
    """
    d = _FUEL_BED_DEPTH_FT.get(fuel_code, 1.0)
    if d <= 0:
        return 0.4
    waf = 1.83 / math.log((20.0 + 0.36 * d) / (0.13 * d))
    return max(0.1, min(0.9, waf))


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


def _gust_blend(speed_kmh: float, gust_kmh, factor: float) -> float:
    """Effective driving wind = sustained + factor·(gust − sustained). The hourly-mean
    wind under-represents the gusts that carry fire runs; blending in the gust closes
    that gap. factor 0 = sustained only. Missing/lower gust falls back to sustained."""
    if not factor or gust_kmh is None:
        return speed_kmh
    return speed_kmh + factor * max(0.0, float(gust_kmh) - speed_kmh)


# Fire-weather regime — a Hot-Dry-Windy-style index HDW = VPD(kPa) × wind(m/s). The
# free-spread model (with gusts + spotting) is well calibrated on hot-dry-windy RUN
# days but over-predicts calm/humid days (real fires are held by higher humidity,
# diurnal calming, and suppression the model ignores). So we scale the model's
# aggressiveness by the regime: full on high-HDW days, damped on low-HDW days.
# Thresholds from real-perimeter validation — the worst over-predictor (Soberanes,
# humid coast) had HDW ~7; well-calibrated run days (Carr) ~40–56.
_HDW_LO = 8.0
_HDW_HI = 38.0


def _regime_factor(temp_c, rh_pct, peak_wind_kmh) -> float:
    """Fire-weather aggressiveness in [0,1] from HDW. Unknown weather → 0.5 (neutral)."""
    if temp_c is None or rh_pct is None:
        return 0.5
    svp = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))   # sat. vapour pressure, kPa
    vpd = max(0.0, svp * (1.0 - float(rh_pct) / 100.0))
    hdw = vpd * (peak_wind_kmh / 3.6)
    return max(0.0, min(1.0, (hdw - _HDW_LO) / (_HDW_HI - _HDW_LO)))


async def _build_wind_series(req: PredictRequest, notes: list[str]):
    """
    Produce the wind ForeFire runs on, plus the RAW wind for ML features.

      - supplied wind_series -> hindcast; 3-col [sustained,dir,gust] is blended here,
        2-col [speed,dir] is treated as already-blended (backward compatible)
      - explicit override  -> constant wind, repeated for every step
      - use_forecast_wind  -> HRRR-backed hourly forecast, gust-blended per step
      - otherwise / on error -> constant current wind

    Returns (series [(speed_kmh, dir)] fed to ForeFire, wind_source_label,
    raw_series [[sustained_kmh, dir, gust_kmh], ...] for ml_correction.wind_features).
    """
    n = _steps(req)
    gust_factor = get_settings().wind_gust_factor

    def _fit(seq):
        return seq[:n] if len(seq) >= n else seq + [seq[-1]] * (n - len(seq))

    if req.wind_series:
        raw = []
        for s in req.wind_series:
            if len(s) >= 3:
                raw.append([float(s[0]), float(s[1]), float(s[2])])
            elif len(s) >= 2:
                raw.append([float(s[0]), float(s[1]), float(s[0])])   # no gust column
        if not raw:
            raw = [[15.0, 270.0, 15.0]]
        has_gust = any(len(s) >= 3 for s in req.wind_series)
        if has_gust:                                   # caller sent raw gusts → blend here
            series = [(_gust_blend(r[0], r[2], gust_factor), r[1]) for r in raw]
        else:                                          # already blended by the caller
            series = [(r[0], r[1]) for r in raw]
        series = _fit(series)                          # ForeFire uses n steps; wind
                                                       # FEATURES use the full raw window
                                                       # (matches the training pipeline)
        notes.append(
            f"Supplied historical wind series: start {series[0][0]:.0f} km/h @ "
            f"{series[0][1]:.0f}deg -> end {series[-1][0]:.0f} km/h @ {series[-1][1]:.0f}deg."
        )
        return series, "historical (supplied)", raw

    if req.wind_speed_kmh is not None and req.wind_direction_deg is not None:
        s, d = float(req.wind_speed_kmh), float(req.wind_direction_deg)
        notes.append(f"Wind held constant at {s:.0f} km/h @ {d:.0f}deg (override).")
        return [(s, d)] * n, "override (constant)", [[s, d, s]] * n

    if req.use_forecast_wind:
        try:
            hourly = await weather_svc.forecast_hourly(req.lat, req.lon, int(req.duration_hours) + 1)
            series, raw = [], []
            for k in range(n):
                h = hourly[min(len(hourly) - 1, int((k * req.step_minutes) // 60))]
                sus, dr = float(h["wind_speed_kmh"]), float(h["wind_direction_deg"])
                gu = h.get("wind_gust_kmh")
                series.append((_gust_blend(sus, gu, gust_factor), dr))
                raw.append([sus, dr, float(gu) if gu is not None else sus])
            first, last = series[0], series[-1]
            gust_note = f" (gusts blended, factor {gust_factor:.1f})" if gust_factor else ""
            notes.append(
                f"HRRR-backed forecast wind: start {first[0]:.0f} km/h @ {first[1]:.0f}deg -> "
                f"end {last[0]:.0f} km/h @ {last[1]:.0f}deg{gust_note}."
            )
            return series, "Open-Meteo hourly (HRRR-backed)", raw
        except Exception as exc:
            notes.append(f"Forecast wind unavailable ({exc}); using constant current wind.")

    wx = await weather_svc.current(req.lat, req.lon)
    speed = wx.wind_speed_kmh if wx.wind_speed_kmh is not None else 15.0
    direction = wx.wind_direction_deg if wx.wind_direction_deg is not None else 270.0
    gust = float(wx.wind_gust_kmh) if wx.wind_gust_kmh is not None else speed
    eff = _gust_blend(speed, wx.wind_gust_kmh, gust_factor)
    gust_note = f" (gust-blended {eff:.0f} km/h)" if eff > speed + 0.5 else ""
    notes.append(f"Current wind from {wx.source}: {speed:.0f} km/h @ {direction:.0f}deg (held constant){gust_note}.")
    return [(eff, direction)] * n, wx.source, [[speed, direction, gust]] * n


async def _gather_inputs(req: PredictRequest) -> dict[str, Any]:
    """Resolve the wind series, fuel, and slope, using overrides when provided."""
    notes: list[str] = []

    wind_series, wind_source, raw_wind = await _build_wind_series(req, notes)
    wind_feats = ml_correction.wind_features(raw_wind)   # canonical, matches training

    fuel_code = req.fuel_model or await fuel_svc.fuel_at(req.lat, req.lon)
    fuel_params = fuel_svc.get_params(fuel_code)
    if req.fuel_model is None:
        notes.append(f"Fuel model: {fuel_params['code']} ({fuel_params['name']}).")

    slope = req.slope_percent
    uphill_bearing = None
    if slope is None:
        res = await terrain_svc.slope_aspect_at(req.lat, req.lon)
        if res is None:
            slope = 0.0
            notes.append("Slope unavailable; assumed flat (0%).")
        else:
            slope, uphill_bearing = res
            notes.append(f"Local slope {slope:.0f}%, uphill toward {uphill_bearing:.0f}deg.")

    # Fuel moisture. Dead fine-fuel moisture: supplied temperature/RH (hindcast)
    # take priority, else live conditions — dead fine fuels respond quickly to
    # humidity and strongly affect rate of spread. Live moisture is seasonal, keyed
    # to the fire's month (current month for a live forecast; season_month override
    # for a hindcast). Best-effort; falls back to a dry default.
    month = req.season_month or datetime.datetime.utcnow().month
    temp_used = rh_used = None
    if req.temperature_c is not None and req.relative_humidity is not None:
        temp_used, rh_used = req.temperature_c, req.relative_humidity
        moisture = _fuel_moisture_from_weather(req.temperature_c, req.relative_humidity, month, req.lat)
        notes.append(
            f"Fuel moisture from supplied RH {req.relative_humidity:.0f}% → "
            f"1-h dead {moisture['ones'] * 100:.0f}%; live herbaceous "
            f"{moisture['liveh'] * 100:.0f}% (month {month})."
        )
    else:
        try:
            wx = await weather_svc.current(req.lat, req.lon)
            temp_used, rh_used = wx.temperature_c, wx.relative_humidity
            moisture = _fuel_moisture_from_weather(wx.temperature_c, wx.relative_humidity, month, req.lat)
            if wx.relative_humidity is not None:
                notes.append(
                    f"Fuel moisture from {wx.source}: RH {wx.relative_humidity:.0f}% "
                    f"→ 1-h dead {moisture['ones'] * 100:.0f}%; live herbaceous "
                    f"{moisture['liveh'] * 100:.0f}% (month {month})."
                )
            else:
                notes.append("Humidity unavailable; used default dry fuel moisture.")
        except Exception:
            lh, lw = _seasonal_live_moisture(month, req.lat)
            moisture = dict(_DEFAULT_MOISTURE, liveh=lh, livew=lw)
            notes.append("Live conditions for fuel moisture unavailable; used dry defaults.")

    # Fire-weather regime: damp the model's aggressiveness on calm/humid days (where
    # free-spread over-predicts) and keep it full on hot-dry-windy run days. Scales
    # the driving wind (a suppression/calming proxy) and the spotting reach.
    regime = 0.5
    if get_settings().regime_scaling:
        _rs = get_settings()
        peak_wind = max((s[0] for s in wind_series), default=15.0)
        regime = _regime_factor(temp_used, rh_used, peak_wind)
        wind_mult = _rs.regime_wind_min + (_rs.regime_wind_max - _rs.regime_wind_min) * regime
        if abs(wind_mult - 1.0) > 0.001:
            wind_series = [(s * wind_mult, d) for s, d in wind_series]
        # Spotting reach scales with regime, boosted on the most extreme days (adds
        # lateral fan area where the surface model under-predicts a run).
        spot_scale = regime * (1.0 + (_rs.regime_spot_boost - 1.0) * regime)
        notes.append(
            f"Fire-weather regime {regime:.2f} (0=calm/humid, 1=hot-dry-windy): "
            f"driving wind ×{wind_mult:.2f}, spotting reach ×{spot_scale:.2f}."
        )
    else:
        spot_scale = regime

    # Suppression: a free-spread model over-predicts fires that crews/control lines
    # are holding. Damp the driving wind by a suppression signal — supplied directly
    # (validation: recent growth momentum), else derived from reported containment.
    if get_settings().suppression_scaling:
        supp = req.suppression
        if supp is None and req.percent_contained:
            supp = (req.percent_contained / 100.0) * get_settings().containment_to_suppression
        supp = max(0.0, min(1.0, supp)) if supp is not None else 0.0
        if supp > 0.01:
            supp_mult = 1.0 - get_settings().suppression_damp * supp
            wind_series = [(s * supp_mult, d) for s, d in wind_series]
            notes.append(
                f"Suppression {supp:.2f} (0=free-burning, 1=held): driving wind ×{supp_mult:.2f}."
            )

    origin_lat, origin_lon = req.lat, req.lon
    initial_polygon = None
    ignition = "point"
    if req.ignition_geojson is not None:
        # Hindcast: ignite from the supplied T0 footprint geometry.
        parsed = spread_model.perimeter_to_polygon(req.ignition_geojson)
        if parsed:
            origin_lat, origin_lon, initial_polygon = parsed
            ignition = "supplied-geometry"
            notes.append("Ignited from supplied geometry (hindcast).")
        else:
            notes.append("Supplied ignition geometry unusable.")
    elif req.ignite_from_perimeter:
        try:
            geom = await fires_svc.nearest_perimeter_geometry(req.lat, req.lon, radius_km=8.0)
            parsed = spread_model.perimeter_to_polygon(geom) if geom else None
            if parsed:
                origin_lat, origin_lon, initial_polygon = parsed
                ignition = "perimeter"
                notes.append("Ignited from mapped NIFC perimeter (fire's current footprint).")
            else:
                notes.append("No mapped perimeter available for this fire.")
        except Exception as exc:
            notes.append(f"Perimeter lookup failed ({exc}); ignited from the point.")

    # Spatially-varying fuel AND terrain across the whole domain (fetched together):
    #  - fuel: the LANDFIRE raster so water/urban/rock become non-burnable barriers
    #    the fire stops at (instead of one fuel type filling everything).
    #  - elevation: a real USGS 3DEP DEM so ForeFire derives local slope/aspect per
    #    node (canyons, chimneys, ridges) rather than a single domain-wide plane.
    # Both are best-effort; on failure _run_forefire falls back to a uniform fuel
    # map / a tilted plane respectively.
    domain_half, fire_half = _domain_extents(initial_polygon)
    fuel_grid = elev_grid = None
    try:
        sw_lon, sw_lat = local_meters_to_lonlat(origin_lat, origin_lon, -domain_half, -domain_half)
        ne_lon, ne_lat = local_meters_to_lonlat(origin_lat, origin_lon, domain_half, domain_half)
        _cfg = get_settings()
        want_dem = _cfg.terrain_dem
        fuel_grid, elev_grid = await asyncio.gather(
            fuel_svc.fuel_grid(sw_lon, sw_lat, ne_lon, ne_lat, sample_count=_cfg.fuel_grid_samples),
            terrain_svc.elevation_grid(sw_lon, sw_lat, ne_lon, ne_lat, n=_cfg.elev_grid_n) if want_dem
            else asyncio.sleep(0, result=None),
            return_exceptions=True,
        )
        if isinstance(fuel_grid, Exception):
            fuel_grid = None
        if isinstance(elev_grid, Exception):
            elev_grid = None
        if fuel_grid:
            notes.append(
                f"Fuel map: {fuel_grid['ncols']}×{fuel_grid['nrows']} LANDFIRE grid, "
                f"{fuel_grid['nonburn_pct']:.0f}% non-burnable barrier (water/urban/rock)."
            )
        if elev_grid:
            notes.append(
                f"Terrain: {elev_grid['ncols']}×{elev_grid['nrows']} Copernicus DEM grid, "
                f"{elev_grid['relief_m']:.0f} m relief — ForeFire derives per-node slope/aspect."
            )
    except Exception:
        fuel_grid = elev_grid = None

    return {
        "wind_series": wind_series,
        "wind_source": wind_source,
        "fuel": fuel_params,
        "moisture": moisture,
        "slope_percent": float(slope),
        "uphill_bearing_deg": uphill_bearing,
        "origin_lat": origin_lat,
        "origin_lon": origin_lon,
        "initial_polygon": initial_polygon,
        "domain_half": domain_half,
        "fire_half": fire_half,
        "fuel_grid": fuel_grid,
        "elev_grid": elev_grid,
        "regime": regime,
        "spot_scale": spot_scale,
        "temp_used": temp_used,
        "rh_used": rh_used,
        "wind_feats": wind_feats,
        "ignition": ignition,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# ForeFire helpers
# ---------------------------------------------------------------------------

# Fallback dead/live fuel moisture fractions when live weather is unavailable
# (a dry fire-weather assumption). Live values here are only the last-resort
# fallback; the normal path uses the seasonal curve below.
_DEFAULT_MOISTURE: dict[str, float] = {
    "ones": 0.06, "tens": 0.07, "hundreds": 0.08, "liveh": 0.70, "livew": 0.90,
}

# Representative seasonal live-fuel moisture (fraction of dry weight), by month for
# the Northern Hemisphere, tuned to the WESTERN US fire season: a short spring
# green-up (Mar–May) then rapid curing so that by peak season (Jul–Sep) live
# herbaceous is dry (~35%) and behaves as available fuel. Live woody varies less.
# Live fuel moisture is seasonal, not driven by the current RH — so keying it to the
# fire's month is far better than a fixed year-round value (a fixed 0.70 over-wets
# cured summer fuels and under-predicts peak-season runs; an earlier version that
# kept June green at 0.90 measurably hurt June fires against real perimeters, hence
# the earlier curing here). Not a substitute for live NFMD samples, but a defensible
# default. Southern-Hemisphere months are shifted six months.
_LIVE_HERB_BY_MONTH = [0.50, 0.55, 0.80, 1.10, 1.00, 0.60, 0.40, 0.35, 0.35, 0.45, 0.50, 0.50]
_LIVE_WOODY_BY_MONTH = [0.85, 0.85, 0.95, 1.10, 1.10, 0.90, 0.80, 0.75, 0.75, 0.80, 0.85, 0.85]


def _seasonal_live_moisture(month, lat) -> tuple[float, float]:
    """(live herbaceous, live woody) moisture fractions for a month and latitude."""
    try:
        m = int(month)
    except (TypeError, ValueError):
        m = datetime.datetime.utcnow().month
    if lat is not None and lat < 0:               # Southern Hemisphere: shift 6 months
        m = ((m - 1 + 6) % 12) + 1
    i = max(0, min(11, m - 1))
    return _LIVE_HERB_BY_MONTH[i], _LIVE_WOODY_BY_MONTH[i]


def _fuel_moisture_from_weather(temp_c, rh_pct, month=None, lat=None) -> dict[str, float]:
    """
    Estimate dead + live fuel moisture (fractions). Dead fine-fuel moisture comes
    from temperature and relative humidity via the Simard (1968) equilibrium-
    moisture-content model (the basis of NFDRS fine dead-fuel moisture): dead fine
    fuels equilibrate quickly to the air, so 1-h ≈ EMC and 10-h/100-h lag slightly
    wetter. Live fuel moisture is seasonal (not driven by instantaneous RH), so it
    comes from the month-keyed curve (_seasonal_live_moisture).

    Falls back to dry dead-fuel defaults if temp/RH are missing or implausible, but
    still applies the seasonal live values.
    """
    liveh, livew = _seasonal_live_moisture(month, lat)
    if temp_c is None or rh_pct is None:
        d = dict(_DEFAULT_MOISTURE)
        d["liveh"], d["livew"] = liveh, livew
        return d
    try:
        h = max(1.0, min(100.0, float(rh_pct)))
        t_f = float(temp_c) * 9.0 / 5.0 + 32.0
    except (TypeError, ValueError):
        d = dict(_DEFAULT_MOISTURE)
        d["liveh"], d["livew"] = liveh, livew
        return d

    if h < 10.0:
        emc = 0.03229 + 0.281073 * h - 0.000578 * h * t_f
    elif h < 50.0:
        emc = 2.22749 + 0.160107 * h - 0.014784 * t_f
    else:
        emc = 21.0606 + 0.005565 * h * h - 0.00035 * h * t_f - 0.483199 * h

    fm1 = max(1.0, min(40.0, emc))   # 1-h dead fuel moisture, percent
    return {
        "ones": round(fm1 / 100.0, 4),
        "tens": round(min(40.0, fm1 + 1.0) / 100.0, 4),
        "hundreds": round(min(40.0, fm1 + 2.0) / 100.0, 4),
        "liveh": liveh,
        "livew": livew,
    }


def _met_wind_to_uv(speed_ms: float, from_deg: float) -> tuple[float, float]:
    """
    Meteorological wind (speed in m/s, FROM direction in degrees) →
    Cartesian U (east, m/s) / V (north, m/s).

    Wind blows FROM from_deg, so the velocity vector points in the opposite
    direction: U = -speed·sin(from_rad), V = -speed·cos(from_rad).
    """
    rad = math.radians(from_deg)
    return -speed_ms * math.sin(rad), -speed_ms * math.cos(rad)


def _parse_forefire_fronts(
    print_out: str, origin_lat: float, origin_lon: float
) -> list[list[list[float]]]:
    """
    Parse the string returned by ff.execute("print[]") into a list of GeoJSON
    rings [[lon, lat], ...].  ForeFire encodes fronts as text blocks:

        FireFront ... FireNode[loc=(x,y,z),...] FireNode[...] ...

    Coordinates are in local metres centred on the fire origin; we convert
    each (x_m, y_m) back to (lon, lat).
    """
    rings: list[list[list[float]]] = []
    for chunk in print_out.split("FireFront")[1:]:
        nodes = chunk.split("FireNode")[1:]
        if not nodes:
            continue
        ring: list[list[float]] = []
        for node in nodes:
            m = re.search(r"loc=\(([^,]+),([^,]+),", node)
            if m:
                x_m, y_m = float(m.group(1)), float(m.group(2))
                lon, lat = local_meters_to_lonlat(origin_lat, origin_lon, x_m, y_m)
                ring.append([lon, lat])
        if len(ring) >= 3:
            if ring[0] != ring[-1]:
                ring.append(ring[0])
            rings.append(ring)
    return rings


def _largest_ring_by_area(
    rings: list[list[list[float]]], origin_lat: float, origin_lon: float
) -> tuple[list[float] | None, float]:
    """Pick the ring enclosing the most area (the actual fire extent), and return
    it with that area in km². Node count is a poor proxy — a many-node artefact
    front can out-vote the real perimeter — so we select by area."""
    best_ring: list[float] | None = None
    best_area = 0.0
    for ring in rings:
        a = _ring_area_km2(ring, origin_lat, origin_lon)
        if a > best_area:
            best_area, best_ring = a, ring
    return best_ring, best_area


def _ring_head_km(ring: list[list[float]], origin_lat: float, origin_lon: float) -> float:
    return max(haversine_km(origin_lat, origin_lon, lat, lon) for lon, lat in ring)


def _ring_area_km2(ring: list[list[float]], origin_lat: float, origin_lon: float) -> float:
    """Shoelace area of a [lon,lat] ring via local-metre coordinates."""
    pts = [lonlat_to_local_meters(origin_lat, origin_lon, lon, lat) for lon, lat in ring]
    area = 0.0
    for i in range(len(pts) - 1):
        area += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(area) / 2.0 / 1_000_000.0


def _directional_extents(
    ring: list[list[float]], origin_lat: float, origin_lon: float, toward_deg: float
) -> tuple[float, float, float]:
    """
    Extents of a ring (km) relative to the origin, resolved along the wind:
    (downwind head, upwind backing, max cross-wind). If wind is driving spread,
    downwind should exceed backing; if they're ~equal the growth is isotropic.
    """
    trad = math.radians(toward_deg)
    tx, ty = math.sin(trad), math.cos(trad)   # wind-toward unit (east, north)
    px, py = -ty, tx                           # cross-wind unit
    head = back = 0.0
    cross = 0.0
    for lon, lat in ring:
        ex, nth = lonlat_to_local_meters(origin_lat, origin_lon, lon, lat)
        along = ex * tx + nth * ty
        head = max(head, along)
        back = min(back, along)
        cross = max(cross, abs(ex * px + nth * py))
    return head / 1000.0, -back / 1000.0, cross / 1000.0


# ---------------------------------------------------------------------------
# ForeFire engine runner (synchronous — called via run_in_executor)
# ---------------------------------------------------------------------------

def _seed_firefront(ff, initial_polygon, perim_res: float) -> int:
    """
    Seed the simulation with a real, propagating FireFront and return its node
    count.

    ForeFire advances a front made of ordered FireNode vertices. Seeding with an
    explicit `state=init` front (as the ForeFire examples do) — rather than bare
    `startFire[]` points — is what makes the front actually move; point ignitions
    on a coarse domain can otherwise sit frozen.

    The perimeter is SIMPLIFIED to roughly the working front resolution
    (`perim_res`) with Douglas–Peucker rather than blunt-subsampled: this keeps
    the real shape (corners, fingers, concavities) so the forecast starts from
    the actual perimeter, not a smoothed blob, while keeping the vertex count
    bounded. ForeFire re-meshes to perim_res on the first step regardless, so a
    denser, shape-accurate seed costs no extra steady-state work.

    Nodes are emitted CLOCKWISE with a small outward initial velocity, matching
    ForeFire's farsite_flat.py example (burned interior on the correct side).
    """
    from shapely.geometry.polygon import orient

    front_id, node_id = 2, 4

    if initial_polygon is not None:
        poly = orient(initial_polygon, sign=-1.0)   # force clockwise exterior
        # Simplify to ~half the working resolution: preserves shape features down
        # to perim_res while dropping only redundant collinear vertices.
        try:
            simp = poly.simplify(max(20.0, perim_res * 0.5), preserve_topology=True)
            if simp.geom_type == "Polygon" and not simp.is_empty and len(simp.exterior.coords) >= 4:
                poly = simp
        except Exception:
            pass
        coords = list(poly.exterior.coords)
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) > 1500:                       # hard safety cap on node count
            coords = coords[:: (len(coords) // 1500) + 1]
        cx = sum(p[0] for p in coords) / len(coords)
        cy = sum(p[1] for p in coords) / len(coords)
    else:
        # Small clockwise diamond (N, E, S, W) around the origin point ignition.
        d = 2.0 * float(ff["perimeterResolution"])
        coords = [(0.0, d), (d, 0.0), (0.0, -d), (-d, 0.0)]
        cx = cy = 0.0

    ff.execute(f"FireFront[id={front_id};domain=0;t=0]")
    for x, y in coords:
        ox, oy = x - cx, y - cy
        nrm = math.hypot(ox, oy) or 1.0
        vx, vy = 0.1 * ox / nrm, 0.1 * oy / nrm
        ff.execute(
            f"\tFireNode[domain=0;id={node_id};fdepth=20;kappa=0;"
            f"loc=({x:.2f},{y:.2f},0.);vel=({vx:.4f},{vy:.4f},0);"
            f"t=0;state=init;frontId={front_id}]"
        )
        node_id += 2
    return len(coords)


def _run_forefire(req: PredictRequest, inputs: dict[str, Any]) -> dict[str, Any]:
    """
    Run a ForeFire front-tracking simulation and return a GeoJSON
    FeatureCollection of isochrones (same schema the map already renders).

    Engine setup mirrors ForeFire's canonical real-fuel example
    (tests/python/farsite_flat.py):

      * propagationModel = "Farsite" (Rothermel-family surface spread)
      * fuelsTable       = FARSITE table keyed by LANDFIRE FBFM40 indices, plus a
                           non-burnable barrier (index 999)
      * fuel map         = LANDFIRE fuel grid across the domain, so water/urban/
                           rock become barriers the fire stops at (uniform if the
                           grid fetch failed)
      * moistures.*      = dead fuel moisture from live temperature/humidity
                           (Simard EMC); live moisture from a month-keyed seasonal
                           curve (herbaceous curing)
      * wind             = HRRR-backed hourly forecast, reduced from 10 m to
                           midflame (per-fuel WAF), re-triggered each step so the
                           head bends as the wind shifts
      * elevation        = real USGS 3DEP DEM grid so ForeFire derives per-node
                           slope/aspect (falls back to a tilted plane)
      * ignition         = a real FireFront (see _seed_firefront)

    Domain: a square of local metres centred on the fire, sized to contain the
    perimeter plus spread room (≥ 80 km, up to 500 km across for huge fires),
    on a 100×100 layer grid. Origin (0, 0) is the fire point.

    Runs in a fresh spawned subprocess (see predict()); this configures logging
    locally so per-step progress still reaches the container's stdout.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        import numpy as np
        import pyforefire as ff_module
    except ImportError as exc:
        raise ForeFireUnavailable(f"pyforefire not installed: {exc}")

    origin_lat = inputs["origin_lat"]
    origin_lon = inputs["origin_lon"]
    wind_series: list[tuple[float, float]] = inputs["wind_series"]
    step_seconds = float(req.step_minutes * 60)
    prop_model = get_settings().forefire_propagation_model

    fuel_code = inputs["fuel"]["code"].upper()
    fuel_int = _FBFM40_TO_INT.get(fuel_code, _DEFAULT_FUEL_INT)
    initial_polygon = inputs.get("initial_polygon")

    # --- Domain (local metres, centred on the fire) ---
    # Sized in _domain_extents to contain the perimeter plus spread room (shared
    # with _gather_inputs so the fuel grid covers the same box).
    half = inputs["domain_half"]
    fire_half = inputs["fire_half"]
    grid_n = max(20, get_settings().forefire_grid_n)
    swx = swy = -half
    lx = ly = 2.0 * half
    extent_m = lx
    cell = extent_m / grid_n    # layer cell size (≥ 800 m)

    ff = ff_module.ForeFire()

    # Fuel: the FARSITE standard table extended with a non-burnable barrier
    # (index 999) for water/urban/rock. Passing the table inline (rather than the
    # built-in "STDfarsiteFuelsTable" name) lets us include that barrier row.
    ff["propagationModel"] = prop_model
    ff["fuelsTable"] = FARSITE_FUEL_TABLE
    ff["defaultFuelType"] = float(fuel_int)

    # Dead / live fuel moisture (fraction), derived from live temperature +
    # humidity via Simard EMC in _gather_inputs (dry defaults if unavailable).
    moisture = inputs.get("moisture") or _DEFAULT_MOISTURE
    ff["moistures.ones"] = moisture["ones"]          # 1-h dead
    ff["moistures.tens"] = moisture["tens"]          # 10-h dead
    ff["moistures.hundreds"] = moisture["hundreds"]  # 100-h dead
    ff["moistures.liveh"] = moisture["liveh"]        # live herbaceous
    ff["moistures.livew"] = moisture["livew"]        # live woody

    # Front-tracking tuning. Scale the front resolution to the ACTUAL fire size:
    # a big fire has a long perimeter, and at a fixed fine resolution ForeFire
    # tracks thousands of nodes and spends ~20 s per step, so a 6-hour forecast
    # can't finish inside the time budget (it returns only the first hour or two).
    # Scaling resolution with the fire's radius keeps the node count — and thus
    # the work per step — roughly constant regardless of fire size, so all steps
    # complete. Small fires stay at the 200 m floor; huge ones coarsen to ~2.5 km.
    _s = get_settings()
    perim_res = min(_s.forefire_perim_res_max, max(_s.forefire_perim_res_min, fire_half / _s.forefire_perim_res_div))
    ff["spatialIncrement"] = max(10.0, perim_res / 20.0)
    ff["minimalPropagativeFrontDepth"] = max(100.0, perim_res * 0.5)
    ff["perimeterResolution"] = perim_res
    ff["minSpeed"] = 0.0
    ff["relax"] = 0.5
    ff["smoothing"] = 0
    # Engine-side 10 m→midflame reduction is DISABLED (factor 1.0); we apply the
    # per-fuel WAF once ourselves below. Setting this to 1.0 is what prevents the
    # wind from being double-reduced — do not also reduce it here.
    ff["windReductionFactor"] = 1.0
    ff["bmapLayer"] = 1
    ff["SWx"] = swx
    ff["SWy"] = swy
    ff["Lx"] = lx
    ff["Ly"] = ly

    ff.execute(f"FireDomain[sw=({swx},{swy},0);ne=({swx + lx},{swy + ly},0);t=0.]")
    ff.addLayer("propagation", prop_model, "propagationModel")

    # Fuel map — the LANDFIRE fuel grid across the domain (water/urban/rock as the
    # non-burnable barrier index) when available, else a uniform fuel. Row 0 of
    # the grid is the south edge, matching ForeFire's sw-origin layer convention.
    fuel_grid = inputs.get("fuel_grid")
    if fuel_grid:
        gv = fuel_grid["values"]
        nr, nc = fuel_grid["nrows"], fuel_grid["ncols"]
        fuel_map = np.array(gv, dtype=float).reshape(1, 1, nr, nc)
    else:
        fuel_map = np.full((1, 1, grid_n, grid_n), float(fuel_int))
    ff.addIndexLayer("table", "fuel", swx, swy, 0, lx, ly, 0, fuel_map)

    # Open→midflame wind reduction: the forecast wind is 10 m open wind, but the
    # fire spreads with the slower midflame wind. Multiply the wind fed to the
    # model by the fuel's wind adjustment factor. (We keep the *reported* wind at
    # the 10 m value — this only affects the simulation input.) On top of the WAF
    # we apply the empirical spread-adjustment factor (config.spread_wind_adjust,
    # default 1.0 = raw model — validating against real GeoMAC perimeters showed no
    # systematic over-prediction; see validation/README.md). A per-request waf_scale
    # overrides it. Clamped so it never exceeds the 10 m wind.
    scale = req.waf_scale if req.waf_scale is not None else get_settings().spread_wind_adjust
    waf = max(0.05, min(1.0, _wind_adjustment_factor(fuel_code) * scale))

    # Wind layers, shape (1, 2, ny, nx) per the ForeFire examples. Overwritten
    # each step by trigger[wind;...]; these are just the t=0 values.
    u0, v0 = _met_wind_to_uv(wind_series[0][0] / 3.6 * waf, wind_series[0][1])
    wind_map = np.zeros((2, 2, grid_n, grid_n))
    windU = wind_map[0:1, :, :, :]
    windU[0, 0, :, :] = u0
    windV = wind_map[1:2, :, :, :]
    windV[0, 1, :, :] = v0
    ff.addScalarLayer("windScalDir", "windU", swx, swy, 0, lx, ly, 0, windU)
    ff.addScalarLayer("windScalDir", "windV", swx, swy, 0, lx, ly, 0, windV)

    # Elevation. Preferred: a real USGS 3DEP DEM grid across the domain, so ForeFire
    # computes the local slope AND aspect at every node from true micro-topography
    # (canyons, chimneys, ridges) — the terrain complexity its front-tracking engine
    # is built to exploit. Row 0 of the grid is the south edge, matching the fuel
    # layer and ForeFire's sw-origin convention.
    #
    # Fallback (no DEM grid): a single tilted plane whose gradient magnitude equals
    # the point-sampled slope and whose uphill direction is the point aspect — the
    # right slope strength and upslope direction, but domain-uniform. If aspect is
    # unknown (slope override / lookup failure) we leave it flat.
    elev_grid = inputs.get("elev_grid")
    if elev_grid:
        er, ec = elev_grid["nrows"], elev_grid["ncols"]
        alt_map = np.array(elev_grid["values"], dtype=float).reshape(1, 1, er, ec)
    else:
        slope_frac = inputs["slope_percent"] / 100.0
        uphill = inputs.get("uphill_bearing_deg")
        alt_map = np.zeros((1, 1, grid_n, grid_n))
        if uphill is not None and slope_frac > 0:
            urad = math.radians(uphill)
            ux, uy = math.sin(urad), math.cos(urad)          # unit uphill (east, north)
            xs = swx + (np.arange(grid_n) + 0.5) * cell       # east per column
            ys = swy + (np.arange(grid_n) + 0.5) * cell       # north per row
            gx, gy = np.meshgrid(xs, ys)                       # gx[iy,ix]=east, gy[iy,ix]=north
            alt_map[0, 0] = slope_frac * (gx * ux + gy * uy)
    ff.addScalarLayer("data", "altitude", swx, swy, 0, lx, ly, 0, alt_map)

    # --- Ignition: seed a real propagating FireFront ---
    n_seed = _seed_firefront(ff, initial_polygon, perim_res)
    if fuel_grid:
        fuel_desc = f"grid {fuel_grid['ncols']}x{fuel_grid['nrows']} ({fuel_grid['nonburn_pct']:.0f}% barrier)"
    else:
        fuel_desc = "uniform"
    if elev_grid:
        terrain_desc = f"DEM {elev_grid['ncols']}x{elev_grid['nrows']} ({elev_grid['relief_m']:.0f}m relief)"
    else:
        terrain_desc = (f"tilted plane {inputs['slope_percent']:.0f}%@"
                        + (f"{inputs.get('uphill_bearing_deg'):.0f}deg"
                           if inputs.get("uphill_bearing_deg") is not None else "flat"))
    log.info(
        "ForeFire start: model=%s fuel=%s(%d) map=%s terrain=%s waf=%.2f "
        "steps=%d seed_nodes=%d perim_ignite=%s domain_half=%.0f km",
        prop_model, fuel_code, fuel_int, fuel_desc, terrain_desc, waf,
        len(wind_series), n_seed, initial_polygon is not None, half / 1000.0,
    )

    # --- Simulation loop (wall-clock budgeted) ---
    features: list[dict[str, Any]] = []
    last_ring: list[list[float]] | None = None
    current_t = 0.0
    t0 = time.monotonic()
    budget = get_settings().forefire_time_budget_s

    # Crown-fire spotting enhancement (off by default). Uses the AMBIENT 10 m wind
    # (speed_kmh, not the midflame-reduced wind) since embers are lofted by the wind
    # aloft, and the 1-h dead fuel moisture to gate receptivity. spot_prev carries
    # the previous step's enhanced footprint so the isochrones stay nested.
    spotting_on = get_settings().crown_spotting
    dead_1h = (moisture or {}).get("ones")
    spot_regime = inputs.get("spot_scale", inputs.get("regime", 1.0))   # regime-scaled spotting reach
    spot_prev = None

    for step_idx, (speed_kmh, dir_from) in enumerate(wind_series, start=1):
        elapsed = time.monotonic() - t0
        if step_idx > 1 and elapsed > budget:
            log.warning(
                "ForeFire time budget hit after %.1fs at step %d/%d — returning "
                "partial forecast.", elapsed, step_idx - 1, len(wind_series),
            )
            break

        u, v = _met_wind_to_uv(speed_kmh / 3.6 * waf, dir_from)
        ff.execute(f"trigger[wind;loc=(0.,0.,0.);vel=({u:.4f},{v:.4f},0);t={current_t:.1f}]")
        ff.execute(f"step[dt={step_seconds:.1f}]")
        current_t += step_seconds

        print_out = ff.execute("print[]")
        rings = _parse_forefire_fronts(print_out, origin_lat, origin_lon)
        ring, area_km2 = _largest_ring_by_area(rings, origin_lat, origin_lon)
        if ring is None:
            ring, area_km2 = last_ring, (
                _ring_area_km2(last_ring, origin_lat, origin_lon) if last_ring else 0.0
            )
        # Crown-fire spotting: broaden the surface front with a downwind ember fan
        # (see spotting.py). Recompute area from the enhanced ring.
        if ring is not None and spotting_on:
            try:
                ring, spot_prev = spotting.enhance_ring(
                    ring, origin_lat, origin_lon, speed_kmh, (dir_from + 180.0) % 360.0,
                    fuel_code, dead_1h, spot_prev, intensity=spot_regime,
                )
                area_km2 = _ring_area_km2(ring, origin_lat, origin_lon)
            except Exception as exc:            # spotting is an enhancement — never fail on it
                log.warning("spotting enhancement failed at step %d (%s); using raw front.",
                            step_idx, exc)
        head_km = _ring_head_km(ring, origin_lat, origin_lon) if ring else 0.0
        # Directional diagnostic: downwind vs backing extent. downwind >> backing
        # means wind is driving the spread; ~equal means isotropic growth.
        if ring is not None:
            toward = (dir_from + 180.0) % 360.0
            dw, bk, cr = _directional_extents(ring, origin_lat, origin_lon, toward)
        else:
            dw = bk = cr = 0.0
        log.info(
            "ForeFire step %d/%d @ %.1fs: %d fronts, area=%.2f km², "
            "downwind=%.2f km backing=%.2f km cross=%.2f km, wind=%.0f km/h @ %.0f°",
            step_idx, len(wind_series), time.monotonic() - t0, len(rings),
            area_km2, dw, bk, cr, speed_kmh, dir_from,
        )
        if ring is None:
            continue
        last_ring = ring

        minutes = step_idx * req.step_minutes
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "step": step_idx,
                "minutes": minutes,
                "hours": round(minutes / 60.0, 2),
                "head_distance_km": round(head_km, 3),
                "area_km2": round(area_km2, 3),
                "wind_speed_kmh": round(speed_kmh, 1),
                "wind_from_deg": round(dir_from, 1),
            },
        })

    if not features:
        # No fronts across every step. A genuine seeding failure (no nodes placed)
        # is a real error and always raises.
        if n_seed <= 0:
            raise ForeFireUnavailable(
                "ForeFire could not seed a fire front from the ignition perimeter "
                f"(fuel {fuel_code}, index {fuel_int}). The perimeter geometry may "
                "be invalid or outside the fuel domain."
            )
        if initial_polygon is not None:
            # Seeded from a real perimeter but modeled zero spread — wet/sparse fuel
            # (e.g. TL5 timber litter) under light wind genuinely may not advance over
            # a few hours. Return the CURRENT perimeter as a static forecast so the
            # map still shows the footprint rather than failing.
            static_ring = [
                list(local_meters_to_lonlat(origin_lat, origin_lon, x, y))
                for x, y in initial_polygon.exterior.coords
            ]
            area_km2 = _ring_area_km2(static_ring, origin_lat, origin_lon)
            minutes = len(wind_series) * req.step_minutes
            log.warning(
                "ForeFire modeled no spread for fuel %s (index %d) under light wind — "
                "returning the static current perimeter (%.2f km²) as the forecast.",
                fuel_code, fuel_int, area_km2,
            )
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [static_ring]},
                "properties": {
                    "step": len(wind_series),
                    "minutes": minutes,
                    "hours": round(minutes / 60.0, 2),
                    "head_distance_km": 0.0,
                    "area_km2": round(area_km2, 3),
                    "wind_speed_kmh": round(wind_series[-1][0], 1),
                    "wind_from_deg": round(wind_series[-1][1], 1),
                    "static_no_spread": True,
                },
            })
        else:
            # Ignited from a bare point that caught but never advanced (grass/shrub
            # models like GR2 have a low moisture of extinction, so a humid or calm
            # hour yields zero rate of spread). Nothing to draw — return a valid
            # "no spread" forecast with empty isochrones so the client can say so.
            log.warning(
                "ForeFire produced no fire fronts for fuel %s (index %d) — no spread "
                "under the current winds/fuel moisture. Returning a no-spread forecast.",
                fuel_code, fuel_int,
            )
            return {
                "type": "FeatureCollection",
                "features": [],
                "properties": {
                    "model": f"forefire-{prop_model.lower()}",
                    "steps": 0,
                    "seeded_from_perimeter": False,
                    "no_spread": True,
                    "fuel_code": fuel_code,
                },
            }

    # Report how much the fire grew, but do NOT reject a slow fire. A large
    # perimeter under light wind legitimately only gains a thin rind over a few
    # hours — that's a valid forecast, not an error. We only flag it so the UI
    # can note "modeled spread was minimal".
    areas = [f["properties"]["area_km2"] for f in features]
    heads = [f["properties"]["head_distance_km"] for f in features]
    area_first, area_peak = areas[0], max(areas)
    head_first, head_peak = heads[0], max(heads)
    series = " → ".join(f"{a:.2f}" for a in areas)
    log.info(
        "ForeFire done: area series (km²): %s ; head %.3f → %.3f km",
        series, head_first, head_peak,
    )
    low_spread = (area_peak - area_first) < 0.01 and (head_peak - head_first) < 0.05
    if low_spread:
        log.warning(
            "ForeFire modeled minimal spread for fuel %s (index %d) — light wind "
            "or sparse fuel. Returning the near-static forecast anyway.",
            fuel_code, fuel_int,
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "model": f"forefire-{prop_model.lower()}",
            "steps": len(features),
            "seeded_from_perimeter": initial_polygon is not None,
            "low_spread": low_spread,
        },
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def predict(req: PredictRequest) -> PredictResponse:
    # Containment guard (server-side, so the API can't produce a bogus forecast for
    # a lined fire regardless of which client calls it). A 100%-contained fire has a
    # complete control line and is not expected to spread, so a free-spread forecast
    # is meaningless — skip the simulation (and the expensive input gathering) and
    # return an explicit "contained" response with empty isochrones.
    if req.percent_contained is not None and req.percent_contained >= 100:
        return PredictResponse(
            engine="forefire",
            parameters={
                "origin": {"lat": req.lat, "lon": req.lon},
                "percent_contained": req.percent_contained,
            },
            isochrones={"type": "FeatureCollection", "features": []},
            notes=[
                "This fire is reported 100% contained — it has a complete control "
                "line and no outward spread is expected, so no forecast was run."
            ],
            contained=True,
        )

    # No-perimeter guard: a live map forecast ignites from the fire's official mapped
    # perimeter. Many fires (state/prescribed, freshly reported) have only an incident
    # point + acreage and no perimeter polygon — a point-seeded forecast for them is
    # misleading, so we decline to forecast rather than fabricate one. (Hindcasts that
    # pass an explicit ignition_geojson are exempt.)
    if req.ignite_from_perimeter and req.ignition_geojson is None:
        try:
            geom = await fires_svc.nearest_perimeter_geometry(req.lat, req.lon, radius_km=8.0)
        except Exception:
            geom = None
        if geom is None or spread_model.perimeter_to_polygon(geom) is None:
            return PredictResponse(
                engine="forefire",
                parameters={"origin": {"lat": req.lat, "lon": req.lon}},
                isochrones={"type": "FeatureCollection", "features": []},
                notes=[
                    "No official mapped perimeter is available for this fire, so its "
                    "spread can't be forecasted. Only fires with a mapped perimeter "
                    "(footprint) can be modeled."
                ],
                no_perimeter=True,
            )

    inputs = await _gather_inputs(req)
    notes = list(inputs["notes"])

    # (The free-spread / worst-case caveat is shown once in the UI disclaimer rather
    # than repeated as a per-forecast note.)

    if get_settings().crown_spotting:
        notes.append(
            "Crown-fire ember spotting is enabled: the surface footprint is broadened "
            "with a downwind ember fan in crownable fuels under dry, windy conditions."
        )

    if not _forefire_available():
        raise ForeFireUnavailable(
            "pyforefire is not installed. Run the backend via Docker "
            "(see backend/Dockerfile) or build ForeFire from source "
            "(see docs/FOREFIRE_SETUP.md)."
        )

    # Run the simulation in a fresh spawned subprocess so ForeFire's global C++
    # state starts clean — otherwise every fire after the first inherits the
    # first fire's domain and returns an identical (wrong) prediction. A new
    # single-use process per request is the reliable way to isolate a native,
    # non-reentrant library. The hard timeout guards against a runaway step.
    loop = asyncio.get_running_loop()
    hard_timeout = get_settings().forefire_time_budget_s + 60.0
    pool = ProcessPoolExecutor(max_workers=1, mp_context=_MP_SPAWN)
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(pool, _run_forefire, req, inputs),
            timeout=hard_timeout,
        )
    except asyncio.TimeoutError:
        raise ForeFireUnavailable(
            f"ForeFire simulation exceeded {hard_timeout:.0f}s and was abandoned. "
            "The fire perimeter is likely very large; try again or reduce the "
            "forecast duration."
        )
    finally:
        # Don't block the event loop waiting on the (possibly still-running)
        # child; it is single-use and will exit on its own.
        pool.shutdown(wait=False, cancel_futures=True)

    # No spread at all AND nothing to draw (a bare-point ignition that caught but the
    # fuel doesn't propagate under the current winds/moisture, e.g. grass on a humid
    # hour). Valid outcome, not an error — return empty isochrones with a plain-
    # language note so the map can say "no spread expected" instead of a failure.
    if result.get("properties", {}).get("no_spread"):
        series = inputs["wind_series"]
        wind_kmh = round(series[0][0], 0)
        fm1 = inputs.get("moisture", {}).get("ones")
        detail = f"winds near {wind_kmh:.0f} km/h"
        if fm1 is not None:
            detail += f" and ~{fm1 * 100:.0f}% dead fuel moisture"
        notes.insert(
            0,
            f"No spread expected: {inputs['fuel']['name']} ({inputs['fuel']['code']}) "
            f"does not propagate under the current conditions ({detail}).",
        )
        return PredictResponse(
            engine="forefire",
            parameters=_params(req, inputs),
            isochrones={"type": "FeatureCollection", "features": []},
            notes=notes,
        )

    # Seeded from a real perimeter but modeled zero spread: the forecast is the
    # fire's current (static) perimeter. Note it and skip ML rescaling below.
    feats = result.get("features") or []
    static = bool(feats) and all(
        (f.get("properties") or {}).get("static_no_spread") for f in feats
    )
    if static:
        notes.append(
            "ForeFire modeled no outward spread over this horizon — the fuel here "
            "(wet/sparse timber litter) under the light forecast winds isn't expected "
            "to advance. The forecast shown is the fire's current perimeter."
        )
    elif result.get("properties", {}).get("low_spread"):
        notes.append(
            "ForeFire modeled only minimal spread here — light forecast winds "
            "and/or sparse fuel. The isochrones are close together."
        )

    # ML residual correction (Phase 5, opt-in): rescale the footprint by the learned
    # observed/forecast factor. Features are always computed (and returned under
    # parameters.ml_features for parity checks); the rescale only happens when enabled
    # and the model + libs are present.
    feat = _ml_features(req, inputs)
    # Skip ML rescaling when the physics engine modeled a genuinely static (no-spread)
    # fire: there's nothing to correct, and inflating a stationary perimeter with a MOS
    # factor would fabricate spread the fuel/wind don't support.
    if not static and get_settings().ml_correction and ml_correction.available():
        factor = ml_correction.correction_factor(feat)
        if factor is not None:
            ml_correction.scale_isochrones(result, factor, inputs["origin_lat"],
                                           inputs["origin_lon"], inputs.get("initial_polygon"))
            notes.append(
                f"ML residual correction applied: forecast area ×{factor:.2f} "
                "(learned observed/forecast; see validation/train_correction.py)."
            )

    params = _params(req, inputs)
    params["ml_features"] = feat
    return PredictResponse(engine="forefire", parameters=params, isochrones=result, notes=notes)


def _ml_features(req: PredictRequest, inputs: dict[str, Any]) -> dict[str, Any]:
    """Forecast-time features for the ML correction, matching the training columns
    (validation/build_features.py) as closely as the server can. Note: `momentum` and
    `peak_gust` aren't available server-side (live forecasts) → NaN, which the model
    handles; this is the known feature-parity gap between the Phase-4 evaluation
    (exact features) and live inference."""
    nan = float("nan")
    mean_wind, peak_wind, peak_gust, dir_cons = inputs.get("wind_feats") or (nan, nan, nan, nan)
    poly = inputs.get("initial_polygon")
    t0_km2 = (poly.area / 1_000_000.0) if poly is not None else 1.0
    t, rh = inputs.get("temp_used"), inputs.get("rh_used")
    if t is not None and rh is not None:
        vpd = max(0.0, 0.6108 * math.exp(17.27 * t / (t + 237.3)) * (1 - rh / 100.0))
        hdw = vpd * (peak_wind / 3.6) if peak_wind == peak_wind else nan
    else:
        vpd = hdw = nan
    return {
        "month": req.season_month or datetime.datetime.utcnow().month,
        "log_t0": math.log(max(t0_km2, 1.0)),
        "momentum": req.momentum if req.momentum is not None else nan,
        "mean_wind": mean_wind, "peak_wind": peak_wind, "peak_gust": peak_gust,
        "dir_consistency": dir_cons,
        "temp_c": t if t is not None else nan,
        "rh": rh if rh is not None else nan,
        "vpd": vpd, "hdw": hdw,
        "fuel_model": inputs["fuel"]["code"], "slope_pct": inputs["slope_percent"],
        "horizon_h": req.duration_hours,
    }


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
        "wind_speed_kmh": round(start_speed, 1),
        "wind_direction_deg": round(start_dir, 1),
        "wind_end_speed_kmh": round(end_speed, 1),
        "wind_end_direction_deg": round(end_dir, 1),
        "fuel_model": inputs["fuel"]["code"],
        "fuel_name": inputs["fuel"]["name"],
        "slope_percent": inputs["slope_percent"],
    }
