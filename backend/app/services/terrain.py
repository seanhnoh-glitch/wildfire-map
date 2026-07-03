"""
Terrain — slope drives fire spread (fire runs uphill). We estimate the local
slope by sampling elevation at the point and a few neighbors and taking the
steepest gradient.

Elevation source: Open-Meteo elevation API (keyless, global, backed by Copernicus
DEM). For a production/ForeFire pipeline you would instead clip a USGS 3DEP DEM
tile around the fire; this point-estimate is enough to modulate the built-in model.
"""
import httpx

from .geo import haversine_km

OPEN_METEO_ELEV = "https://api.open-meteo.com/v1/elevation"


async def slope_percent_at(lat: float, lon: float, sample_m: float = 120.0) -> float | None:
    """
    Estimate slope (rise/run as a percent) near a point by sampling elevation on
    a small cross (N/S/E/W) and taking the maximum gradient. Returns None on
    failure so callers can fall back to a flat-ground assumption.
    """
    dlat = (sample_m / 111_000.0)
    dlon = (sample_m / 111_000.0)  # good enough for a slope estimate
    pts = [
        (lat, lon),
        (lat + dlat, lon), (lat - dlat, lon),
        (lat, lon + dlon), (lat, lon - dlon),
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

    center = elev[0]
    max_slope = 0.0
    for (plat, plon), e in zip(pts[1:], elev[1:]):
        run_m = haversine_km(lat, lon, plat, plon) * 1000.0
        if run_m <= 0:
            continue
        slope = abs(e - center) / run_m
        max_slope = max(max_slope, slope)
    return round(max_slope * 100.0, 1)
