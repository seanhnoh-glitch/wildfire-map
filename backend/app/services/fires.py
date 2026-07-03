"""
Active wildfire data for the US, from three complementary public sources:

  1. NIFC WFIGS incident *points*  -> where fires are (fast to appear, authoritative)
  2. NIFC WFIGS incident *perimeters* -> mapped fire footprints (lag 12-24h)
  3. NASA FIRMS satellite *hotspots*  -> raw thermal detections (needs a free key)

All three are free. Only FIRMS needs a key. Everything here is async httpx so the
API can fan out to the sources concurrently.
"""
import asyncio
import math
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import get_settings
from ..schemas import Fire
from .geo import haversine_km

# WFIGS current incident locations (points), ArcGIS FeatureServer layer 0.
WFIGS_POINTS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Incident_Locations_Current/FeatureServer/0/query"
)
# WFIGS current interagency perimeters (polygons).
WFIGS_PERIMS_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)
# NASA FIRMS area API: VIIRS S-NPP, last 24h, CSV.
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Filter dispatch noise: keep a fire only if it is sizeable OR brand-new/unsized.
MIN_SIGNIFICANT_ACRES = 10.0
NEW_FIRE_HOURS = 12.0


def _hours_since(epoch_ms: Optional[float]) -> Optional[float]:
    if not epoch_ms:
        return None
    discovered = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    return (datetime.now(timezone.utc) - discovered).total_seconds() / 3600.0


def _iso(epoch_ms: Optional[float]) -> Optional[str]:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()


def _is_significant(size_acres: Optional[float], discovery_ms: Optional[float]) -> bool:
    if size_acres is not None:
        return size_acres >= MIN_SIGNIFICANT_ACRES
    age = _hours_since(discovery_ms)
    return age is not None and age <= NEW_FIRE_HOURS


async def _fetch_points(client: httpx.AsyncClient, lat: float, lon: float, radius_km: float) -> list[Fire]:
    params = {
        "where": "IncidentTypeCategory = 'WF' AND FireOutDateTime IS NULL",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "distance": str(radius_km * 1000),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join([
            "OBJECTID", "IncidentName", "IncidentSize", "DiscoveryAcres",
            "PercentContained", "FireDiscoveryDateTime", "POOCounty", "POOState",
        ]),
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    resp = await client.get(WFIGS_POINTS_URL, params=params)
    resp.raise_for_status()
    data = resp.json()

    fires: list[Fire] = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        f_lon, f_lat = coords[0], coords[1]
        if f_lat is None or f_lon is None:
            continue
        size = props.get("IncidentSize")
        if size is None:
            size = props.get("DiscoveryAcres")
        discovery_ms = props.get("FireDiscoveryDateTime")
        if not _is_significant(size, discovery_ms):
            continue
        fires.append(Fire(
            id=str(props.get("OBJECTID") or f"{f_lat:.4f},{f_lon:.4f}"),
            name=(props.get("IncidentName") or "Unnamed incident").strip(),
            lat=f_lat, lon=f_lon,
            distance_km=round(haversine_km(lat, lon, f_lat, f_lon), 2),
            size_acres=size,
            percent_contained=props.get("PercentContained"),
            discovery_time=_iso(discovery_ms),
            county=props.get("POOCounty"),
            state=props.get("POOState"),
        ))
    fires.sort(key=lambda f: f.distance_km)
    return fires


async def _fetch_perimeters(client: httpx.AsyncClient, lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    params = {
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "distance": str(radius_km * 1000),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,poly_IncidentName,poly_GISAcres",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    try:
        resp = await client.get(WFIGS_PERIMS_URL, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        # Perimeters are best-effort; a failure here shouldn't sink the request.
        return {"type": "FeatureCollection", "features": []}


async def _fetch_hotspots(client: httpx.AsyncClient, lat: float, lon: float, radius_km: float) -> Optional[dict[str, Any]]:
    key = get_settings().firms_map_key
    if not key:
        return None
    # FIRMS area API takes a bounding box: west,south,east,north.
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    bbox = f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}"
    url = f"{FIRMS_URL}/{key}/VIIRS_SNPP_NRT/{bbox}/1"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return _firms_csv_to_geojson(resp.text)
    except Exception:
        return None


def _firms_csv_to_geojson(csv_text: str) -> dict[str, Any]:
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return {"type": "FeatureCollection", "features": []}
    header = [h.strip() for h in lines[0].split(",")]
    idx = {name: i for i, name in enumerate(header)}
    features = []
    for row in lines[1:]:
        cells = row.split(",")
        try:
            lat = float(cells[idx["latitude"]])
            lon = float(cells[idx["longitude"]])
        except (KeyError, ValueError, IndexError):
            continue
        props = {}
        for name in ("bright_ti4", "confidence", "frp", "acq_date", "acq_time"):
            if name in idx and idx[name] < len(cells):
                props[name] = cells[idx[name]].strip()
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


async def nearby(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    """
    Fetch incidents, perimeters, and hotspots concurrently. Perimeters and
    hotspots are best-effort; incident points are the primary signal.
    """
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        points, perims, hotspots = await asyncio.gather(
            _fetch_points(client, lat, lon, radius_km),
            _fetch_perimeters(client, lat, lon, radius_km),
            _fetch_hotspots(client, lat, lon, radius_km),
        )
    return {"fires": points, "perimeters": perims, "hotspots": hotspots}


async def all_active(min_acres: float = 10.0, limit: int = 2000) -> list[Fire]:
    """
    All current US wildfire incidents at/above `min_acres`, nationwide (no spatial
    filter) — for showing every ongoing fire on the map at once. Points only
    (no perimeters/hotspots) to keep the payload light. distance_km is 0 here
    since there is no reference point; the client computes distance if it has one.
    """
    params = {
        "where": (
            "IncidentTypeCategory = 'WF' AND FireOutDateTime IS NULL "
            f"AND IncidentSize >= {min_acres}"
        ),
        "outFields": ",".join([
            "OBJECTID", "IncidentName", "IncidentSize", "PercentContained",
            "FireDiscoveryDateTime", "POOCounty", "POOState",
        ]),
        "orderByFields": "IncidentSize DESC",
        "resultRecordCount": limit,
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(WFIGS_POINTS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    fires: list[Fire] = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        f_lon, f_lat = coords[0], coords[1]
        if f_lat is None or f_lon is None:
            continue
        fires.append(Fire(
            id=str(props.get("OBJECTID") or f"{f_lat:.4f},{f_lon:.4f}"),
            name=(props.get("IncidentName") or "Unnamed incident").strip(),
            lat=f_lat, lon=f_lon, distance_km=0.0,
            size_acres=props.get("IncidentSize"),
            percent_contained=props.get("PercentContained"),
            discovery_time=_iso(props.get("FireDiscoveryDateTime")),
            county=props.get("POOCounty"), state=props.get("POOState"),
        ))
    return fires


async def all_perimeters(min_acres: float = 100.0, limit: int = 1500) -> dict[str, Any]:
    """
    All current US fire perimeters at/above `min_acres`, nationwide, as a GeoJSON
    FeatureCollection — for drawing every mapped footprint on the overview map.
    Geometry is simplified server-side (maxAllowableOffset) to keep the payload
    reasonable; small fires usually have no mapped perimeter anyway.
    """
    params = {
        "where": f"poly_GISAcres >= {min_acres}",
        "outFields": "OBJECTID,poly_IncidentName,poly_GISAcres",
        "returnGeometry": "true",
        "maxAllowableOffset": "0.005",   # ~500 m simplification (degrees at 4326)
        "outSR": "4326",
        "resultRecordCount": limit,
        "f": "geojson",
    }
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(WFIGS_PERIMS_URL, params=params)
        resp.raise_for_status()
        return resp.json()


async def perimeters_in_bbox(
    west: float, south: float, east: float, north: float,
    min_acres: float = 10.0, offset: float = 0.0, limit: int = 1500,
) -> dict[str, Any]:
    """
    Current fire perimeters intersecting a lon/lat bounding box, as GeoJSON.
    `offset` is maxAllowableOffset in degrees (0 = full resolution); the caller
    sets it to about a pixel's worth so a zoomed-in view stays crisp while the
    payload stays small.
    """
    params = {
        "where": f"poly_GISAcres >= {min_acres}",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID,poly_IncidentName,poly_GISAcres",
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": limit,
        "f": "geojson",
    }
    if offset and offset > 0:
        params["maxAllowableOffset"] = str(offset)
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(WFIGS_PERIMS_URL, params=params)
        resp.raise_for_status()
        return resp.json()


async def nearest_perimeter_geometry(lat: float, lon: float, radius_km: float = 10.0) -> Optional[dict[str, Any]]:
    """Return the single closest perimeter polygon geometry, if any, for ignition."""
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        fc = await _fetch_perimeters(client, lat, lon, radius_km)
    feats = fc.get("features") or []
    if not feats:
        return None
    return feats[0].get("geometry")
