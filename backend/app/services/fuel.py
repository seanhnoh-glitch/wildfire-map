"""
Fuel data — the single most important input to any fire-spread model.

fuel_at() queries the live LANDFIRE FBFM40 raster at a point and returns the
Scott & Burgan (2005) fuel-model code (e.g. "GR2"); get_params() attaches the
model's descriptive name. ForeFire keys its simulation off the code (it looks the
fuel's physical parameters up in its own STDfarsiteFuelsTable), so that is all we
need here. Both are best-effort: if LANDFIRE is unreachable or the pixel is
non-burnable, fuel_at() returns None and get_params() falls back to GR2 so a
forecast still runs.

LANDFIRE FBFM40 ImageServer (LANDFIRE 2022 / LF2.3.0, CONUS):
  https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer
  Public, no key. The `identify` op returns the raster pixel value — the integer
  FBFM40 code (e.g. 102 = GR2) — which we map back to a Scott & Burgan code.
"""
import json
from typing import Optional

import httpx

from .fuel_table import BARRIER_FUEL_INDEX

# LANDFIRE 2022 Scott & Burgan 40 fire behavior fuel model image service.
LANDFIRE_FBFM40_IMAGESERVER = (
    "https://lfps.usgs.gov/arcgis/rest/services/"
    "Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer"
)
LANDFIRE_FBFM40_IDENTIFY = LANDFIRE_FBFM40_IMAGESERVER + "/identify"
LANDFIRE_FBFM40_GETSAMPLES = LANDFIRE_FBFM40_IMAGESERVER + "/getSamples"

# Default fuel index for grid cells with no LANDFIRE sample (rare) — GR2 grass.
_GRID_DEFAULT_INT = 102

# Used when LANDFIRE is unavailable or returns a non-burnable/unknown class in a
# region where we still want a demonstrable forecast.
DEFAULT_FUEL = "GR2"

# Full Scott & Burgan 40 name table (matches LANDFIRE / ForeFire STDfarsiteFuels).
FBFM40_NAMES: dict[str, str] = {
    "GR1": "Short, sparse, dry climate grass", "GR2": "Low load, dry climate grass",
    "GR3": "Low load, very coarse, humid climate grass", "GR4": "Moderate load, dry climate grass",
    "GR5": "Low load, humid climate grass", "GR6": "Moderate load, humid climate grass",
    "GR7": "High load, dry climate grass", "GR8": "High load, very coarse, humid climate grass",
    "GR9": "Very high load, humid climate grass",
    "GS1": "Low load, dry climate grass-shrub", "GS2": "Moderate load, dry climate grass-shrub",
    "GS3": "Moderate load, humid climate grass-shrub", "GS4": "High load, humid climate grass-shrub",
    "SH1": "Low load, dry climate shrub", "SH2": "Moderate load, dry climate shrub",
    "SH3": "Moderate load, humid climate shrub", "SH4": "Low load, humid climate timber-shrub",
    "SH5": "High load, dry climate shrub", "SH6": "Low load, humid climate shrub",
    "SH7": "Very high load, dry climate shrub", "SH8": "High load, humid climate shrub",
    "SH9": "Very high load, humid climate shrub",
    "TU1": "Light load, dry climate timber-grass-shrub", "TU2": "Moderate load, humid climate timber-shrub",
    "TU3": "Moderate load, humid climate timber-grass-shrub", "TU4": "Dwarf conifer with understory",
    "TU5": "Very high load, dry climate timber-shrub",
    "TL1": "Low load, compact conifer litter", "TL2": "Low load broadleaf litter",
    "TL3": "Moderate load conifer litter", "TL4": "Small downed logs",
    "TL5": "High load conifer litter", "TL6": "High load broadleaf litter",
    "TL7": "Large downed logs", "TL8": "Long-needle litter", "TL9": "Very high load broadleaf litter",
    "SB1": "Low load activity fuel", "SB2": "Moderate load activity or low load blowdown",
    "SB3": "High load activity fuel or moderate load blowdown", "SB4": "High load blowdown",
}

# FBFM40 burnable code families and the integer base each starts at, so we can
# turn a LANDFIRE raster pixel value (e.g. 102) back into a code (e.g. "GR2").
_FBFM40_FAMILIES: dict[str, tuple[int, int]] = {
    "GR": (101, 9), "GS": (121, 4), "SH": (141, 9),
    "TU": (161, 5), "TL": (181, 9), "SB": (201, 4),
}


def _fbfm40_int_to_code(value: int) -> Optional[str]:
    """Map a LANDFIRE FBFM40 raster integer to its Scott & Burgan code.
    Returns None for non-burnable (91–99) or out-of-range values."""
    for prefix, (base, count) in _FBFM40_FAMILIES.items():
        if base <= value < base + count:
            return f"{prefix}{value - base + 1}"
    return None


def get_params(fuel_code: Optional[str]) -> dict:
    """Return {"code", "name"} for a fuel code, falling back to the default model
    (GR2) for an unknown/None code."""
    if fuel_code and fuel_code in FBFM40_NAMES:
        return {"code": fuel_code, "name": FBFM40_NAMES[fuel_code]}
    return {"code": DEFAULT_FUEL, "name": FBFM40_NAMES[DEFAULT_FUEL]}


async def fuel_at(lat: float, lon: float) -> Optional[str]:
    """
    Best-effort LANDFIRE fuel-model code at a point. Returns a Scott & Burgan
    code string (e.g. 'GR2') or None if the lookup fails or the pixel is
    non-burnable / no-data (letting the caller fall back to the default).

    The LANDFIRE FBFM40 ImageServer `identify` op returns the raster pixel value
    as an integer code string (e.g. {"value": "102"}); we map that to a code.
    """
    params = {
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "returnGeometry": "false",
        "returnCatalogItems": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(LANDFIRE_FBFM40_IDENTIFY, params=params)
            resp.raise_for_status()
            value = resp.json().get("value")
    except Exception:
        return None

    if value in (None, "", "NoData"):
        return None
    try:
        return _fbfm40_int_to_code(int(float(value)))
    except (TypeError, ValueError):
        return None


def _fuel_value_to_index(value) -> int:
    """LANDFIRE raster pixel value → ForeFire fuel index: burnable FBFM40 codes
    (101–204) pass through; non-burnable (91–99) and no-data become the barrier."""
    try:
        iv = int(float(value))
    except (TypeError, ValueError):
        return BARRIER_FUEL_INDEX
    return iv if 101 <= iv <= 204 else BARRIER_FUEL_INDEX


async def fuel_grid(
    west: float, south: float, east: float, north: float, sample_count: int = 900
) -> Optional[dict]:
    """
    A coarse grid of ForeFire fuel indices over a lon/lat bbox, from the LANDFIRE
    FBFM40 ImageServer `getSamples` op (one call, ~30×30 for a square bbox — about
    2–3 km cells over a typical fire domain). Burnable fuels are kept; water,
    urban, rock and no-data become the non-burnable barrier so the fire stops at
    them.

    Returns {"values": [[int]] (row 0 = south, col 0 = west), "ncols", "nrows",
    "nonburn_pct"} or None on failure (caller falls back to a uniform fuel map).
    """
    geom = {"xmin": west, "ymin": south, "xmax": east, "ymax": north,
            "spatialReference": {"wkid": 4326}}
    params = {
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryEnvelope",
        "sampleCount": str(sample_count),
        "returnFirstValueOnly": "true",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(LANDFIRE_FBFM40_GETSAMPLES, params=params)
            resp.raise_for_status()
            samples = resp.json().get("samples", [])
    except Exception:
        return None
    if not samples:
        return None

    pts = []
    for s in samples:
        loc = s.get("location") or {}
        x, y = loc.get("x"), loc.get("y")
        if x is not None and y is not None:
            pts.append((round(x, 5), round(y, 5), s.get("value")))
    xs = sorted({p[0] for p in pts})
    ys = sorted({p[1] for p in pts})   # ascending → row 0 = south
    if len(xs) < 2 or len(ys) < 2:
        return None
    xi = {x: i for i, x in enumerate(xs)}
    yi = {y: i for i, y in enumerate(ys)}
    ncols, nrows = len(xs), len(ys)
    grid = [[_GRID_DEFAULT_INT] * ncols for _ in range(nrows)]
    nonburn = 0
    for x, y, v in pts:
        code = _fuel_value_to_index(v)
        grid[yi[y]][xi[x]] = code
        if code == BARRIER_FUEL_INDEX:
            nonburn += 1
    return {
        "values": grid,
        "ncols": ncols,
        "nrows": nrows,
        "nonburn_pct": 100.0 * nonburn / max(1, len(pts)),
    }
