"""
Terrain — slope drives fire spread (fire runs uphill). We estimate the local
slope by sampling elevation at the point and a few neighbors and taking the
steepest gradient.

Elevation source: Open-Meteo elevation API (keyless, global, backed by Copernicus
DEM). For a production/ForeFire pipeline you would instead clip a USGS 3DEP DEM
tile around the fire; this point-estimate is enough to modulate the built-in model.
"""
import math

import httpx

from .geo import haversine_km

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"


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
    return slope_percent, uphill_bearing
