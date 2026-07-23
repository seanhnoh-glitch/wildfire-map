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
import asyncio
import json
from typing import Optional

import httpx

from . import geocache
from .fuel_table import BARRIER_FUEL_INDEX

# LANDFIRE Scott & Burgan 40 fire-behavior fuel-model image services. LANDFIRE ships
# these as separate regional products, so CONUS and Alaska have their own ImageServers
# (both keyed by the same FBFM40 encoding). We pick the right one per point via
# _landfire_base — otherwise Alaska fires (outside the CONUS product) got no fuel data.
LANDFIRE_FBFM40_CONUS = (
    "https://lfps.usgs.gov/arcgis/rest/services/"
    "Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer"
)
LANDFIRE_FBFM40_AK = (
    "https://lfps.usgs.gov/arcgis/rest/services/"
    "Landfire_LF2023/LF2023_FBFM40_AK/ImageServer"
)

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


def in_conus(lat: float, lon: float) -> bool:
    """Rough CONUS bounding box."""
    return 24.0 <= lat <= 49.6 and -125.5 <= lon <= -66.5


def in_alaska(lat: float, lon: float) -> bool:
    """Rough Alaska bounding box, WEST of the 141° Yukon border so Canadian Yukon/NWT
    (which the CWFIS FBP grid covers) isn't misrouted to the LANDFIRE Alaska product."""
    return 51.0 <= lat <= 72.0 and -170.0 <= lon <= -141.0


def _landfire_base(lat: float, lon: float) -> Optional[str]:
    """The LANDFIRE FBFM40 ImageServer covering a point (CONUS or Alaska), or None if
    the point is outside LANDFIRE — in which case the caller uses the CWFIS FBP grid."""
    if in_conus(lat, lon):
        return LANDFIRE_FBFM40_CONUS
    if in_alaska(lat, lon):
        return LANDFIRE_FBFM40_AK
    return None


def in_landfire(lat: float, lon: float) -> bool:
    """True where LANDFIRE has coverage (US CONUS or Alaska) → use fuel_at / fuel_grid;
    elsewhere (Canada) the caller uses fuel_at_ca / fuel_grid_ca."""
    return _landfire_base(lat, lon) is not None


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
    base = _landfire_base(lat, lon)
    if base is None:
        return None                        # outside LANDFIRE (e.g. Canada) — caller uses CWFIS
    params = {
        "geometry": json.dumps({"x": lon, "y": lat, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryPoint",
        "returnGeometry": "false",
        "returnCatalogItems": "false",
        "f": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(base + "/identify", params=params)
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
    base = _landfire_base((south + north) / 2.0, (west + east) / 2.0)
    if base is None:
        return None                        # outside LANDFIRE (e.g. Canada) — caller uses CWFIS
    ckey = f"{west:.4f},{south:.4f},{east:.4f},{north:.4f},{sample_count}"
    cached = geocache.get("fuelgrid", ckey)
    if cached is not None:
        return cached

    geom = {"xmin": west, "ymin": south, "xmax": east, "ymax": north,
            "spatialReference": {"wkid": 4326}}
    params = {
        "geometry": json.dumps(geom),
        "geometryType": "esriGeometryEnvelope",
        "sampleCount": str(sample_count),
        "returnFirstValueOnly": "true",
        "f": "json",
    }
    # Retry a few times: a transient LANDFIRE timeout would otherwise silently drop
    # the fuel grid and fall back to a UNIFORM fuel map (no water/urban barriers,
    # and non-deterministic run-to-run). One reliable real grid matters more here.
    samples = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
                resp = await client.get(base + "/getSamples", params=params)
                resp.raise_for_status()
                samples = resp.json().get("samples", [])
            if samples:
                break
        except Exception:
            samples = None
        if attempt < 2:
            await asyncio.sleep(0.6 * (attempt + 1))
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
    result = {
        "values": grid,
        "ncols": ncols,
        "nrows": nrows,
        "nonburn_pct": 100.0 * nonburn / max(1, len(pts)),
    }
    geocache.put("fuelgrid", ckey, result)
    return result


# --------------------------------------------------------------------------- #
# Canada — CWFIS/CFFDRS FBP fuel types (LANDFIRE is CONUS-only)
# --------------------------------------------------------------------------- #
# LANDFIRE stops at the US border, so Canadian forecasts otherwise ran on a uniform
# grass fuel with NO water barriers (fire crossing lakes). NRCan's national CFFDRS FBP
# fuel-type grid is the Canadian equivalent — it carries the boreal fuel types AND
# water / non-fuel. We read it from the CWFIS GeoServer WMS (a rendered grid whose
# palette colours map 1:1 to fuel classes) and translate each class to the closest
# Scott & Burgan FBFM40 index ForeFire's FARSITE table understands.
CWFIS_FBP_WMS = "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/wms"
CWFIS_FBP_LAYER = "public:cffdrs_fbp_fuel_types_100m"

# CFFDRS FBP class → representative FBFM40 index, matched on SURFACE-fire behaviour.
# Boreal conifer's surface fuel is needle/moss LITTER, so C-2/3/4/5 map to TL3 (moderate
# conifer litter) — NOT a high-load timber-SHRUB like TU5, whose very high load makes the
# surface fire spread like heavy brush and, over a 24 h free-spread horizon, blow up to
# absurd sizes. The fast boreal CROWN-fire behaviour is added separately by the crown-
# spotting enhancement on dry/windy days (TL/TU fuels are crownable). Open conifer
# (C-1/C-7) and mixedwood → TU1 (light timber-grass-shrub); deciduous (D) → TL2 broadleaf
# litter; grass (O-1a) → GR2. Water / non-fuel → the non-burnable barrier.
_FBP_QTY_TO_CODE: dict[int, str | None] = {
    1: "TU1", 2: "TL3", 3: "TL3", 4: "TL3", 5: "TL3", 7: "TU1",
    11: "TL2", 13: "TL2", 31: "GR2",
    101: None, 102: None, 105: None,               # non-fuel / water / vegetated non-fuel
    415: "TU1", 625: "TU1", 650: "TU1", 675: "TU1",
}
_CODE_TO_FBFM40_INT = {"TU1": 161, "TL3": 183, "TL2": 182, "GR2": 102}

# FBP legend colour (r,g,b) → ForeFire fuel index (or the non-burnable barrier).
_FBP_COLOR_TO_INDEX: dict[tuple, int] = {}
for _hex, _qty in {
    "D1FF73": 1, "226633": 2, "83C795": 3, "70A800": 4, "DFB8E6": 5, "700CF2": 7,
    "C4BD97": 11, "897044": 13, "FFFFBE": 31, "828282": 101, "73DFFF": 102,
    "CCCCCC": 105, "FFD37F": 415, "FFC55A": 650, "FFB121": 675,
}.items():
    _rgb = tuple(int(_hex[i:i + 2], 16) for i in (0, 2, 4))
    _code = _FBP_QTY_TO_CODE.get(_qty)
    _FBP_COLOR_TO_INDEX[_rgb] = _CODE_TO_FBFM40_INT[_code] if _code else BARRIER_FUEL_INDEX
_FBP_PALETTE = list(_FBP_COLOR_TO_INDEX.keys())


def _nearest_fbp_index(rgb: tuple) -> int:
    """Map a rendered pixel colour to the nearest FBP palette colour's fuel index.
    A stray blended edge pixel (rare — the raster renders nearest-neighbour) falls
    back to the default grass index rather than a wrong barrier."""
    best, best_d = None, 1_000_000
    for p in _FBP_PALETTE:
        d = (rgb[0] - p[0]) ** 2 + (rgb[1] - p[1]) ** 2 + (rgb[2] - p[2]) ** 2
        if d < best_d:
            best_d, best = d, p
    return _FBP_COLOR_TO_INDEX[best] if best_d < 1200 else _GRID_DEFAULT_INT


async def fuel_at_ca(lat: float, lon: float) -> Optional[str]:
    """Best-effort CFFDRS FBP fuel code at a Canadian point, as an FBFM40 code (e.g.
    'TL3'), via a WMS GetFeatureInfo pixel query. None on failure / water / non-fuel."""
    span = 0.02
    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetFeatureInfo",
        "layers": CWFIS_FBP_LAYER, "query_layers": CWFIS_FBP_LAYER, "srs": "EPSG:4326",
        "bbox": f"{lon - span},{lat - span},{lon + span},{lat + span}",
        "width": "101", "height": "101", "x": "50", "y": "50", "info_format": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(CWFIS_FBP_WMS, params=params)
            resp.raise_for_status()
            feats = resp.json().get("features") or []
        qty = int(feats[0]["properties"]["CFFDRS_FBP_Fuel_Type"]) if feats else None
    except Exception:
        return None
    return _FBP_QTY_TO_CODE.get(qty) if qty is not None else None


async def fuel_grid_ca(
    west: float, south: float, east: float, north: float, sample_count: int = 900
) -> Optional[dict]:
    """
    Canada's CFFDRS FBP fuel grid over a lon/lat bbox, as ForeFire fuel indices with
    water / non-fuel as the non-burnable barrier — the LANDFIRE-equivalent for Canada.
    Rendered by the CWFIS WMS at ~sqrt(sample_count) per side and colour-decoded here.
    Best-effort: returns None on any failure (caller falls back to a uniform map).

    Returns {"values": [[int]] (row 0 = south), "ncols", "nrows", "nonburn_pct"}.
    """
    n = max(2, int(sample_count ** 0.5))
    ckey = f"{west:.4f},{south:.4f},{east:.4f},{north:.4f},{n}"
    cached = geocache.get("fuelgrid_ca", ckey)
    if cached is not None:
        return cached
    try:
        from PIL import Image
    except Exception:
        return None
    import io

    params = {
        "service": "WMS", "version": "1.1.1", "request": "GetMap",
        "layers": CWFIS_FBP_LAYER, "styles": "", "srs": "EPSG:4326",
        "bbox": f"{west},{south},{east},{north}", "width": str(n), "height": str(n),
        "format": "image/png", "transparent": "true",
    }
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
                resp = await client.get(CWFIS_FBP_WMS, params=params)
                resp.raise_for_status()
                im = Image.open(io.BytesIO(resp.content)).convert("RGBA")
            break
        except Exception:
            im = None
        if attempt < 2:
            await asyncio.sleep(0.6 * (attempt + 1))
    if im is None or im.size != (n, n):
        return None

    grid = [[_GRID_DEFAULT_INT] * n for _ in range(n)]
    nonburn = 0
    px = im.load()
    for py in range(n):                       # PNG row 0 = north (top); flip → row 0 = south
        for cx in range(n):
            r, g, b, a = px[cx, py]
            idx = _GRID_DEFAULT_INT if a < 128 else _nearest_fbp_index((r, g, b))
            grid[n - 1 - py][cx] = idx
            if idx == BARRIER_FUEL_INDEX:
                nonburn += 1
    result = {"values": grid, "ncols": n, "nrows": n,
              "nonburn_pct": 100.0 * nonburn / (n * n)}
    geocache.put("fuelgrid_ca", ckey, result)
    return result
