"""
Terrain — slope drives fire spread (fire runs uphill). We estimate the local
slope by sampling elevation at the point and a few neighbors and taking the
steepest gradient.

Elevation source: Open-Meteo elevation API (keyless, global, backed by Copernicus
DEM). For a production/ForeFire pipeline you would instead clip a USGS 3DEP DEM
tile around the fire; this point-estimate is enough to modulate the built-in model.
"""
import asyncio
import math

import httpx

from . import geocache
from .geo import haversine_km

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"
# Open-Meteo elevation accepts up to this many coordinates per request (Copernicus
# DEM, keyless). We batch a DEM grid across that limit.
_ELEV_MAX_COORDS = 100


async def slope_aspect_at(
    lat: float, lon: float, sample_m: float = 120.0
) -> tuple[float, float] | None:
    """
    Estimate (slope_percent, uphill_bearing_deg) at a point from a central-
    difference elevation gradient sampled on a small N/S/E/W cross:

      - slope_percent: rise/run × 100 of the best-fit plane (steepest ascent).
      - uphill_bearing_deg: compass bearing (0=N, 90=E) of steepest ascent —
        the direction fire is pushed uphill.

    Returns None if the elevation lookup fails, so callers can assume flat ground.
    """
    ckey = f"{lat:.4f},{lon:.4f},{sample_m}"
    cached = geocache.get("slope", ckey)
    if cached is not None:
        return tuple(cached) if cached else None

    dlat = sample_m / 111_000.0
    dlon = sample_m / 111_000.0
    pts = [
        (lat, lon),
        (lat + dlat, lon), (lat - dlat, lon),   # N, S
        (lat, lon + dlon), (lat, lon - dlon),   # E, W
    ]
    lats = ",".join(f"{p[0]:.6f}" for p in pts)
    lons = ",".join(f"{p[1]:.6f}" for p in pts)
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(OPEN_METEO_ELEV, params={"latitude": lats, "longitude": lons})
            resp.raise_for_status()
            elev = resp.json().get("elevation", [])
    except Exception:
        return None

    if len(elev) < 5 or any(e is None for e in elev):
        return None

    e_n, e_s, e_e, e_w = elev[1], elev[2], elev[3], elev[4]
    run_ns = haversine_km(lat + dlat, lon, lat - dlat, lon) * 1000.0  # N↔S distance
    run_ew = haversine_km(lat, lon + dlon, lat, lon - dlon) * 1000.0  # E↔W distance
    if run_ns <= 0 or run_ew <= 0:
        return None

    g_north = (e_n - e_s) / run_ns   # rise per metre toward north
    g_east = (e_e - e_w) / run_ew    # rise per metre toward east
    slope_percent = round(math.hypot(g_east, g_north) * 100.0, 1)
    uphill_bearing = round(math.degrees(math.atan2(g_east, g_north)) % 360.0, 1)
    geocache.put("slope", ckey, [slope_percent, uphill_bearing])
    return slope_percent, uphill_bearing


async def _fetch_elevations(coords: list[tuple[float, float]], attempts: int = 3) -> list | None:
    """
    Elevations (m) for up to _ELEV_MAX_COORDS (lat, lon) points, in input order.
    Retries a few times so a transient timeout doesn't silently drop the DEM (which
    would make the whole grid fall back to a flat tilted plane, non-deterministically).
    """
    params = {
        "latitude": ",".join(f"{la:.5f}" for la, _ in coords),
        "longitude": ",".join(f"{lo:.5f}" for _, lo in coords),
    }
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
                resp = await client.get(OPEN_METEO_ELEV, params=params)
                resp.raise_for_status()
                elev = resp.json().get("elevation")
                if elev:
                    return elev
        except Exception:
            pass
        if attempt < attempts - 1:
            await asyncio.sleep(0.6 * (attempt + 1))
    return None


async def elevation_grid(
    west: float, south: float, east: float, north: float, n: int = 28
) -> dict | None:
    """
    An n×n grid of ground elevations (metres) over a lon/lat bbox, sampled from the
    Open-Meteo elevation API (keyless, Copernicus DEM) in ≤100-point batches fetched
    concurrently.

    ForeFire's front-tracking engine derives the local slope AND aspect at every
    node from its altitude layer, so feeding it a real DEM lets it capture the
    ridges, valleys and slopes that steer a fire — instead of the single domain-wide
    tilted plane a point-sampled slope produces. Best-effort: returns None on any
    failure (the caller falls back to the tilted plane).

    Returns {"values": [[m]] (row 0 = south, col 0 = west), "ncols", "nrows",
    "relief_m"} — same row/col convention as fuel_grid so the two layers align.
    """
    n = max(2, int(n))
    ckey = f"{west:.4f},{south:.4f},{east:.4f},{north:.4f},{n}"
    cached = geocache.get("elevgrid", ckey)
    if cached is not None:
        return cached

    # Cell CENTRES, west→east (columns) and south→north (rows, so row 0 = south).
    xs = [west + (east - west) * (i + 0.5) / n for i in range(n)]
    ys = [south + (north - south) * (j + 0.5) / n for j in range(n)]
    coords = [(y, x) for y in ys for x in xs]   # row-major, south first

    chunks = [coords[k:k + _ELEV_MAX_COORDS] for k in range(0, len(coords), _ELEV_MAX_COORDS)]
    results = await asyncio.gather(*[_fetch_elevations(c) for c in chunks])
    elev: list = []
    for res in results:
        if not res:
            return None            # a batch failed → fall back to the tilted plane
        elev.extend(res)
    if len(elev) != n * n:
        return None

    grid = [list(elev[r * n:(r + 1) * n]) for r in range(n)]   # row 0 = south
    flat = [e for e in elev if e is not None]
    if not flat:
        return None
    # Fill the occasional no-data cell (e.g. offshore) with the grid mean.
    mean = sum(flat) / len(flat)
    for r in range(n):
        for c in range(n):
            if grid[r][c] is None:
                grid[r][c] = mean
    result = {"values": grid, "ncols": n, "nrows": n,
              "relief_m": round(max(flat) - min(flat), 1)}
    geocache.put("elevgrid", ckey, result)
    return result
