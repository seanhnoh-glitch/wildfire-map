"""
Fuel data — the single most important input to any fire-spread model.

Two responsibilities:
  1. FUEL_MODELS: a lookup from Scott & Burgan (2005) 40-fuel-model codes to the
     parameters the built-in spread model needs (a reference head rate-of-spread
     and a wind-response exponent). These are approximate, literature-informed
     values suitable for a research prototype, NOT operational fire behavior.
  2. fuel_at(): query the live LANDFIRE fuel raster at a point. Best-effort; if
     the service is unreachable it returns a sensible default so prediction still
     runs. This is the seam where you plug in a full LANDFIRE clip for ForeFire.

LANDFIRE FBFM40 ImageServer:
  https://lfps.usgs.gov / https://landfire.gov  (public, no key)
"""
from typing import Optional

import httpx

# LANDFIRE 2022 Scott & Burgan 40 fire behavior fuel model image service (identify).
LANDFIRE_FBFM40_IDENTIFY = (
    "https://landfire.gov/arcgis/rest/services/Landfire/US_230/MapServer/identify"
)

# Reference head rate of spread (meters/minute) under a nominal 20 km/h wind on
# flat ground, plus a dimensionless wind-response factor. Grass spreads fast and
# is very wind-driven; timber litter is slow. Non-burnable models spread at 0.
# Values are ballpark, drawn from Scott & Burgan (2005) behavior classes.
FUEL_MODELS: dict[str, dict] = {
    # Non-burnable
    "NB1": {"name": "Urban", "ros_ref": 0.0, "wind_factor": 0.0},
    "NB8": {"name": "Open water", "ros_ref": 0.0, "wind_factor": 0.0},
    "NB9": {"name": "Bare ground", "ros_ref": 0.0, "wind_factor": 0.0},
    # Grass (GR) — fast, highly wind-driven
    "GR1": {"name": "Short, sparse dry climate grass", "ros_ref": 4.0, "wind_factor": 1.0},
    "GR2": {"name": "Low load dry climate grass", "ros_ref": 9.0, "wind_factor": 1.1},
    "GR3": {"name": "Low load, very coarse grass", "ros_ref": 11.0, "wind_factor": 1.1},
    "GR4": {"name": "Moderate load dry climate grass", "ros_ref": 15.0, "wind_factor": 1.2},
    "GR5": {"name": "Low load humid climate grass", "ros_ref": 13.0, "wind_factor": 1.15},
    # Grass-shrub (GS)
    "GS1": {"name": "Low load dry climate grass-shrub", "ros_ref": 7.0, "wind_factor": 1.0},
    "GS2": {"name": "Moderate load dry climate grass-shrub", "ros_ref": 10.0, "wind_factor": 1.05},
    # Shrub (SH) — moderate, wind-driven
    "SH1": {"name": "Low load dry climate shrub", "ros_ref": 4.0, "wind_factor": 0.9},
    "SH2": {"name": "Moderate load dry climate shrub", "ros_ref": 6.0, "wind_factor": 0.95},
    "SH5": {"name": "High load dry climate shrub", "ros_ref": 12.0, "wind_factor": 1.0},
    "SH7": {"name": "Very high load dry climate shrub", "ros_ref": 14.0, "wind_factor": 1.0},
    # Timber-understory (TU)
    "TU1": {"name": "Low load dry timber-grass-shrub", "ros_ref": 3.0, "wind_factor": 0.7},
    "TU5": {"name": "Very high load dry timber-shrub", "ros_ref": 5.0, "wind_factor": 0.7},
    # Timber litter (TL) — slow, less wind-driven
    "TL1": {"name": "Low load compact conifer litter", "ros_ref": 1.0, "wind_factor": 0.4},
    "TL3": {"name": "Moderate load conifer litter", "ros_ref": 1.5, "wind_factor": 0.4},
    "TL5": {"name": "High load conifer litter", "ros_ref": 2.0, "wind_factor": 0.45},
    # Slash-blowdown (SB)
    "SB1": {"name": "Low load activity fuel", "ros_ref": 3.0, "wind_factor": 0.6},
    "SB3": {"name": "High load activity fuel", "ros_ref": 6.0, "wind_factor": 0.7},
}

# Used when LANDFIRE is unavailable or returns a non-burnable/unknown class in a
# region where we still want a demonstrable forecast.
DEFAULT_FUEL = "GR2"


def get_params(fuel_code: Optional[str]) -> dict:
    """Return spread params for a fuel code, falling back to the default model."""
    if fuel_code and fuel_code in FUEL_MODELS:
        return {"code": fuel_code, **FUEL_MODELS[fuel_code]}
    return {"code": DEFAULT_FUEL, **FUEL_MODELS[DEFAULT_FUEL]}


async def fuel_at(lat: float, lon: float) -> Optional[str]:
    """
    Best-effort LANDFIRE fuel-model code at a point. Returns a Scott & Burgan
    code string (e.g. 'GR2') or None if the lookup fails. The ArcGIS identify
    call returns the raster attribute; the exact field name varies by release,
    so we scan the returned attributes for anything that looks like an FBFM40
    code before giving up.
    """
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "sr": "4326",
        "tolerance": "1",
        "mapExtent": f"{lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01}",
        "imageDisplay": "100,100,96",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(LANDFIRE_FBFM40_IDENTIFY, params=params)
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except Exception:
        return None

    for r in results:
        attrs = r.get("attributes", {})
        for value in attrs.values():
            code = str(value).strip().upper()
            if code in FUEL_MODELS:
                return code
    return None
