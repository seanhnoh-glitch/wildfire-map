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
import time
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
# Canadian active wildfires (CWFIS-derived, hosted as an ArcGIS FeatureServer of
# incident *points*). Fields: Fire_Name, Latitude, Longitude, Hectares__Ha_,
# Stage_of_Control (OC/BH/UC) + SoC_Text, Start_Date (epoch ms), Agency (province).
# Canada reports a stage of control, not a containment %, so percent_contained
# stays None for these and stage_of_control carries the status.
CA_FIRES_URL = (
    "https://services.arcgis.com/fFPraSowbm3gs7ek/arcgis/rest/services/"
    "ActiveWildfiresInCanada/FeatureServer/0/query"
)
# Canadian fire perimeters: CWFIS Fire M3 current-day estimates (polygons derived
# from buffered season-to-date satellite hotspots), served as GeoServer WFS. These
# are SATELLITE ESTIMATES, not agency-surveyed lines like the US perimeters — NRCan
# labels them non-operational — but they're the best national footprint layer.
# Attributes: uid, hcount, firstdate, lastdate, area (hectares).
CA_PERIMS_URL = (
    "https://cwfis.cfs.nrcan.gc.ca/geoserver/public/ows"
    "?service=WFS&version=1.0.0&request=GetFeature"
    "&typeName=public:m3_polygons_current&outputFormat=application/json&srsName=EPSG:4326"
)
HECTARES_TO_ACRES = 2.47105

# NASA FIRMS area API (CSV). We use VIIRS aboard NOAA-20 — the primary
# operational VIIRS satellite, with a denser/timelier NRT feed than the aging
# Suomi-NPP. Day range is 2, not 1: NRT for the current day lags a few hours, so
# a 1-day window often misses hotspots that are actually dated "yesterday".
FIRMS_URL = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
FIRMS_SOURCE = "VIIRS_NOAA20_NRT"
FIRMS_DAY_RANGE = 2

# Filter dispatch noise: keep a fire only if it is sizeable OR brand-new/unsized.
MIN_SIGNIFICANT_ACRES = 10.0
NEW_FIRE_HOURS = 12.0

# Hotspots are only shown when they belong to a KNOWN fire (see hotspots_in_bbox):
# within this buffer of a mapped perimeter, or within this radius of an active
# incident point. This drops FIRMS thermal anomalies (industrial/agricultural heat,
# sun-warmed ground) that aren't the wildfire. Perimeters lag 12-24h, so the buffer
# is generous enough not to clip the active front that has spread past the last map.
HOTSPOT_PERIM_BUFFER_KM = 5.0
HOTSPOT_POINT_RADIUS_KM = 10.0


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


async def _fetch_perimeters(
    client: httpx.AsyncClient, lat: float, lon: float, radius_km: float, offset: float = 0.0
) -> dict[str, Any]:
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
    # Optional server-side simplification (maxAllowableOffset in degrees). A large
    # fire's full-resolution perimeter is huge and slow; a simplified footprint is
    # far faster and plenty for containment/avoidance checks.
    if offset and offset > 0:
        params["maxAllowableOffset"] = str(offset)
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
    url = f"{FIRMS_URL}/{key}/{FIRMS_SOURCE}/{bbox}/{FIRMS_DAY_RANGE}"
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return _firms_csv_to_geojson(resp.text)
    except Exception:
        return None


async def _points_in_bbox(
    client: httpx.AsyncClient, west: float, south: float, east: float, north: float
) -> list[tuple[float, float]]:
    """Active WF incident point coordinates (lon, lat) intersecting a bbox."""
    params = {
        "where": "IncidentTypeCategory = 'WF' AND FireOutDateTime IS NULL",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "OBJECTID",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    try:
        resp = await client.get(WFIGS_POINTS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []
    pts: list[tuple[float, float]] = []
    for feat in data.get("features", []):
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2 and coords[0] is not None and coords[1] is not None:
            pts.append((float(coords[0]), float(coords[1])))
    return pts


def _filter_hotspots_to_fires(
    hot_fc: dict[str, Any], perim_fc: dict[str, Any], points: list[tuple[float, float]]
) -> dict[str, Any]:
    """
    Keep only hotspots that belong to a known fire: inside (or within
    HOTSPOT_PERIM_BUFFER_KM of) a mapped perimeter, or within
    HOTSPOT_POINT_RADIUS_KM of an active incident point. Everything else is
    treated as an unrelated thermal anomaly and dropped.
    """
    feats = hot_fc.get("features") or []
    if not feats:
        return hot_fc

    from shapely.geometry import Point, shape
    from shapely.ops import unary_union

    polys = []
    for f in perim_fc.get("features") or []:
        geom = f.get("geometry")
        if not geom:
            continue
        try:
            poly = shape(geom)
            if not poly.is_valid:
                poly = poly.buffer(0)
            polys.append(poly)
        except Exception:
            continue
    # Buffer in degrees of latitude (~111 km/deg); a few km is well within the
    # accuracy needed to associate a detection with the fire it belongs to.
    region = unary_union(polys).buffer(HOTSPOT_PERIM_BUFFER_KM / 111.0) if polys else None

    kept = []
    for f in feats:
        try:
            lon, lat = f["geometry"]["coordinates"][:2]
        except Exception:
            continue
        near = region is not None and region.intersects(Point(lon, lat))
        if not near:
            near = any(
                haversine_km(lat, lon, plat, plon) <= HOTSPOT_POINT_RADIUS_KM
                for plon, plat in points
            )
        if near:
            kept.append(f)
    return {"type": "FeatureCollection", "features": kept}


async def hotspots_in_bbox(
    west: float, south: float, east: float, north: float
) -> dict[str, Any]:
    """
    FIRMS satellite thermal hotspots within a lon/lat bounding box, as GeoJSON,
    restricted to detections that belong to a known fire (near a mapped perimeter
    or an active incident point) so unrelated thermal anomalies don't appear as
    stray dots. Returns an empty FeatureCollection if no key is configured or the
    fetch fails (best-effort overlay). Used by the map to show where fires are
    actively burning right now, in the current viewport.
    """
    empty = {"type": "FeatureCollection", "features": []}
    key = get_settings().firms_map_key
    if not key:
        return empty
    bbox = f"{west},{south},{east},{north}"
    url = f"{FIRMS_URL}/{key}/{FIRMS_SOURCE}/{bbox}/{FIRMS_DAY_RANGE}"
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            hot_fc = _firms_csv_to_geojson(resp.text)
        except Exception:
            return empty
        if not hot_fc.get("features"):
            return hot_fc
        # Associate detections with known fires. Both lookups are best-effort: if
        # either fails we fall back to showing the (confidence-filtered) hotspots
        # rather than hiding real fire activity.
        perim_fc, points = await asyncio.gather(
            perimeters_in_bbox(west, south, east, north, min_acres=1.0),
            _points_in_bbox(client, west, south, east, north),
            return_exceptions=True,
        )
        if isinstance(perim_fc, Exception):
            perim_fc = {"type": "FeatureCollection", "features": []}
        if isinstance(points, Exception):
            points = []
        if not (perim_fc.get("features") or points):
            return hot_fc
        return _filter_hotspots_to_fires(hot_fc, perim_fc, points)


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
        # Drop FIRMS low-confidence detections: for VIIRS these are the most likely
        # false alarms (sun-warmed ground, marginal thermal anomalies) and show up
        # as stray dots away from any real fire. Keep nominal ("n") and high ("h").
        if str(props.get("confidence", "")).lower() in ("l", "low"):
            continue
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
    All current US + Canadian wildfire incidents at/above `min_acres`, nationwide
    (no spatial filter) — for showing every ongoing fire on the map at once. Points
    only (no perimeters/hotspots) to keep the payload light. Fetches both countries
    concurrently; if one source fails the other is still returned. distance_km is 0
    here since there is no reference point; the client computes distance if it has one.
    """
    us, ca = await asyncio.gather(
        all_active_us(min_acres=min_acres, limit=limit),
        all_active_ca(min_acres=min_acres, limit=limit),
        return_exceptions=True,
    )
    fires: list[Fire] = []
    if isinstance(us, list):
        fires.extend(us)
    if isinstance(ca, list):
        fires.extend(ca)
    # If BOTH sources errored, surface the US error (the primary source) so the
    # router still returns a clear 502 rather than an empty list.
    if not isinstance(us, list) and not isinstance(ca, list):
        raise us
    fires.sort(key=lambda f: (f.size_acres or 0), reverse=True)
    return fires[:limit]


async def all_active_us(min_acres: float = 10.0, limit: int = 2000) -> list[Fire]:
    """All current US wildfire incidents (NIFC WFIGS points), largest first."""
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
            country="US",
        ))
    return fires


async def all_active_ca(min_acres: float = 10.0, limit: int = 2000) -> list[Fire]:
    """
    All current Canadian wildfire incidents (CWFIS ActiveWildfiresInCanada points),
    largest first. Sizes come in hectares (converted to acres); containment is a
    categorical stage of control, not a percentage.
    """
    min_ha = min_acres / HECTARES_TO_ACRES
    params = {
        "where": f"Hectares__Ha_ >= {min_ha}",
        "outFields": ",".join([
            "OBJECTID", "Fire_Name", "Hectares__Ha_", "SoC_Text",
            "Start_Date", "Agency",
        ]),
        "orderByFields": "Hectares__Ha_ DESC",
        "resultRecordCount": limit,
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(CA_FIRES_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return _ca_fires_from_geojson(data)


def _ca_fires_from_geojson(data: dict[str, Any]) -> list[Fire]:
    """Map the Canadian FeatureServer GeoJSON into our Fire schema."""
    fires: list[Fire] = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or [None, None]
        f_lon, f_lat = coords[0], coords[1]
        if f_lat is None or f_lon is None:
            continue
        ha = props.get("Hectares__Ha_")
        size_acres = round(ha * HECTARES_TO_ACRES, 1) if ha is not None else None
        # Fire_Name is often an agency code (e.g. "2025_BC_2025-C22340"); tidy the
        # separators so it reads a little better in the UI.
        raw_name = (props.get("Fire_Name") or "Unnamed incident").strip()
        name = raw_name.replace("_", " ")
        fires.append(Fire(
            id=f"CA{props.get('OBJECTID')}",
            name=name,
            lat=f_lat, lon=f_lon, distance_km=0.0,
            size_acres=size_acres,
            percent_contained=None,   # Canada reports stage of control, not a %
            discovery_time=_iso(props.get("Start_Date")),
            county=None, state=props.get("Agency"),
            country="CA",
            stage_of_control=props.get("SoC_Text"),
        ))
    return fires


# --- Perimeter snapshot cache ---------------------------------------------
# Every map pan/zoom used to hit the WFIGS ArcGIS service directly and uncached,
# so a burst of panning (or the 5-min auto-refresh racing a pan) could trip
# ArcGIS rate limits / timeouts — the request then returned empty and the map's
# perimeter overlay blanked. Instead we fetch the WHOLE nationwide perimeter set
# ONCE, cache it in memory, and answer every /perimeters/all and /perimeters/bbox
# request from that snapshot (filtering by acreage / viewport in-process). The
# upstream service is hit at most once per _PERIM_TTL_S regardless of how much the
# user pans, and if a refresh fails we keep serving the last good snapshot
# (stale-while-error) so the overlay never blanks.
_PERIM_TTL_S = 90.0
_PERIM_MIN_ACRES = 1.0           # include even small fires so nothing is missing
_PERIM_OFFSET = 0.0004           # ~40 m simplification: crisp at any app zoom, ~0.5 MB total
_PERIM_FETCH_LIMIT = 4000
# Parallel index of (bbox=(w,s,e,n), acres, feature) for fast viewport filtering.
_perim_index: Optional[list[tuple[tuple[float, float, float, float], float, dict]]] = None
_perim_fetched_at = 0.0
_perim_lock = asyncio.Lock()


def _geom_bbox(geom: Optional[dict]) -> Optional[tuple[float, float, float, float]]:
    """(west, south, east, north) of any GeoJSON geometry, or None if empty."""
    if not geom:
        return None
    xs: list[float] = []
    ys: list[float] = []

    def walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                xs.append(c[0]); ys.append(c[1])
            else:
                for sub in c:
                    walk(sub)

    walk(geom.get("coordinates"))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


async def _fetch_all_perimeters_raw() -> dict[str, Any]:
    params = {
        "where": f"poly_GISAcres >= {_PERIM_MIN_ACRES}",
        "outFields": "OBJECTID,poly_IncidentName,poly_GISAcres",
        "returnGeometry": "true",
        "maxAllowableOffset": str(_PERIM_OFFSET),
        "outSR": "4326",
        "resultRecordCount": _PERIM_FETCH_LIMIT,
        "f": "geojson",
    }
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(WFIGS_PERIMS_URL, params=params)
        resp.raise_for_status()
        return resp.json()


async def _ca_perimeters(min_acres: float = 0.0) -> dict[str, Any]:
    """
    Canadian Fire M3 perimeter estimates as GeoJSON, normalised to the SAME
    properties the US perimeters use (poly_IncidentName, poly_GISAcres) so the map
    styles them identically and the shared snapshot cache / bbox filter treat them
    the same. These are satellite-derived estimates (source=CWFIS-M3), not agency-
    surveyed lines like the US perimeters.
    """
    min_ha = min_acres / HECTARES_TO_ACRES
    async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(CA_PERIMS_URL)
        resp.raise_for_status()
        data = resp.json()

    feats: list[dict[str, Any]] = []
    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if not geom:
            continue
        area_ha = props.get("area")
        if area_ha is not None and area_ha < min_ha:
            continue
        feats.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "OBJECTID": props.get("uid"),
                "poly_IncidentName": None,
                "poly_GISAcres": round(area_ha * HECTARES_TO_ACRES, 1) if area_ha is not None else None,
                "source": "CWFIS-M3",   # satellite-estimated, not agency-surveyed
            },
        })
    return {"type": "FeatureCollection", "features": feats}


async def _perimeter_index() -> list[tuple[tuple[float, float, float, float], float, dict]]:
    """The cached nationwide (US + Canada) perimeter index, refreshed at most every
    _PERIM_TTL_S. On a refresh failure the previous snapshot is kept and served stale
    rather than letting the overlay blank; only a cold-start failure (no snapshot
    yet) raises. The Canadian source is best-effort — if it fails, US perimeters
    still populate the index (and vice versa)."""
    global _perim_index, _perim_fetched_at
    now = time.monotonic()
    if _perim_index is not None and (now - _perim_fetched_at) < _PERIM_TTL_S:
        return _perim_index
    async with _perim_lock:
        now = time.monotonic()
        if _perim_index is not None and (now - _perim_fetched_at) < _PERIM_TTL_S:
            return _perim_index                       # another task refreshed while we waited
        try:
            us_fc, ca_fc = await asyncio.gather(
                _fetch_all_perimeters_raw(),
                _ca_perimeters(),
                return_exceptions=True,
            )
            # A cold start with both sources down has nothing to serve → re-raise.
            if not isinstance(us_fc, dict) and not isinstance(ca_fc, dict):
                raise us_fc if isinstance(us_fc, BaseException) else ca_fc
            merged: list[dict] = []
            if isinstance(us_fc, dict):
                merged.extend(us_fc.get("features") or [])
            if isinstance(ca_fc, dict):
                merged.extend(ca_fc.get("features") or [])
            index = []
            for f in merged:
                bb = _geom_bbox(f.get("geometry"))
                if bb is None:
                    continue
                acres = (f.get("properties") or {}).get("poly_GISAcres") or 0.0
                index.append((bb, float(acres), f))
            _perim_index = index
            _perim_fetched_at = time.monotonic()
        except Exception:
            if _perim_index is None:
                raise                                 # nothing to fall back to on a cold start
            # Keep serving stale data, but retry sooner than a full TTL from now.
            _perim_fetched_at = time.monotonic() - _PERIM_TTL_S + 15.0
        return _perim_index


async def all_perimeters(min_acres: float = 100.0, limit: int = 1500) -> dict[str, Any]:
    """
    All current US fire perimeters at/above `min_acres`, nationwide, as a GeoJSON
    FeatureCollection — for drawing every mapped footprint on the overview map.
    Served from the in-memory snapshot (see _perimeter_index); geometry is
    pre-simplified to ~40 m to keep the payload reasonable.
    """
    index = await _perimeter_index()
    feats = [f for (_bb, acres, f) in index if acres >= min_acres][:limit]
    return {"type": "FeatureCollection", "features": feats}


def _bbox_overlaps(a: tuple[float, float, float, float], w: float, s: float, e: float, n: float) -> bool:
    return not (a[2] < w or a[0] > e or a[3] < s or a[1] > n)


def _snapshot_bbox(index, west, south, east, north, min_acres, limit):
    feats = [
        f for (bb, acres, f) in index
        if acres >= min_acres and _bbox_overlaps(bb, west, south, east, north)
    ][:limit]
    return {"type": "FeatureCollection", "features": feats}


async def perimeters_in_bbox(
    west: float, south: float, east: float, north: float,
    min_acres: float = 10.0, offset: float = 0.0, limit: int = 1500,
) -> dict[str, Any]:
    """
    Current fire perimeters (US + Canada) intersecting a lon/lat bounding box, as
    GeoJSON, served from the single cached nationwide snapshot (see _perimeter_index).
    The snapshot is kept at ~40 m resolution, so zoomed-in views are crisp WITHOUT a
    per-pan live ArcGIS query (which rate-limited under burst and made the overlay
    coarse or blank when it failed). Serving both the map draw AND the forecast button
    from this one source means they can never disagree. `offset` is accepted for API
    compatibility but ignored — the snapshot resolution is fixed.
    """
    index = await _perimeter_index()
    return _snapshot_bbox(index, west, south, east, north, min_acres, limit)


async def has_perimeter_near(lat: float, lon: float, radius_km: float = 8.0) -> bool:
    """True when the fire at (lat, lon) has an official mapped perimeter to ignite
    from. This is the SAME check the /predict no-perimeter guard uses, so the UI's
    forecast button always matches what a forecast would actually do."""
    from . import spread_model
    try:
        geom = await nearest_perimeter_geometry(lat, lon, radius_km=radius_km)
    except Exception:
        return False
    return geom is not None and spread_model.perimeter_to_polygon(geom) is not None


def _pick_perimeter(feats: list, lon: float, lat: float,
                    max_dist_deg: Optional[float] = None) -> Optional[dict[str, Any]]:
    """From candidate perimeter features pick the one CONTAINING the point (a fire's
    own footprint), else the genuinely closest — so we don't seed from a neighbour.
    If `max_dist_deg` is given, a non-containing nearest beyond that distance is
    rejected (returns None), so a fire with no perimeter of its own doesn't pick up a
    distant neighbour's perimeter just because their bounding boxes overlapped."""
    from shapely.geometry import Point, shape
    pt = Point(lon, lat)
    closest_geom = None
    closest_dist = float("inf")
    for feat in feats:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            poly = shape(geom)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            continue
        if poly.contains(pt):
            return geom
        d = pt.distance(poly)
        if d < closest_dist:
            closest_dist, closest_geom = d, geom
    if max_dist_deg is not None and closest_dist > max_dist_deg:
        return None
    return closest_geom


async def _snapshot_perimeter_geometry(
    lat: float, lon: float, radius_km: float = 8.0
) -> Optional[dict[str, Any]]:
    """Perimeter geometry for the fire at (lat, lon) from the cached nationwide
    snapshot (see _perimeter_index) — fast and reliable, and identical to what the
    map draws. None if the snapshot has no perimeter within the radius."""
    index = await _perimeter_index()
    if not index:
        return None
    deg = radius_km / 111.0
    box = (lon - deg, lat - deg, lon + deg, lat + deg)
    cand = [f for (bb, _ac, f) in index if _bbox_overlaps(bb, *box)]
    return _pick_perimeter(cand, lon, lat, max_dist_deg=deg)


async def nearest_perimeter_geometry(
    lat: float, lon: float, radius_km: float = 10.0, offset: float = 0.0
) -> Optional[dict[str, Any]]:
    """
    Return the perimeter geometry belonging to the clicked fire, for ignition.

    Served from the cached nationwide snapshot first (fast, reliable, and identical
    to what the map draws) so the forecast button, the /predict no-perimeter guard,
    and the ignition all agree and don't flap when the live ArcGIS service is slow.
    Falls back to a live point query only if the snapshot has nothing (e.g. a fire
    whose perimeter is below the snapshot's size floor, or a cold cache).
    """
    geom = await _snapshot_perimeter_geometry(lat, lon, radius_km=radius_km)
    if geom is not None:
        return geom
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        fc = await _fetch_perimeters(client, lat, lon, radius_km, offset=offset)
    return _pick_perimeter(fc.get("features") or [], lon, lat)
