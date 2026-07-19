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


async def all_perimeters(min_acres: float = 100.0, limit: int = 1500) -> dict[str, Any]:
    """
    All current US + Canadian fire perimeters at/above `min_acres`, nationwide, as a
    GeoJSON FeatureCollection — for drawing every mapped footprint on the overview
    map. US geometry is simplified server-side to keep the payload reasonable.
    Canadian footprints are best-effort: if that source fails, US perimeters still
    return (and vice versa).
    """
    us, ca = await asyncio.gather(
        _us_all_perimeters(min_acres=min_acres, limit=limit),
        _ca_perimeters(min_acres=min_acres),
        return_exceptions=True,
    )
    return _merge_perimeter_fcs(us, ca)


async def _us_all_perimeters(min_acres: float = 100.0, limit: int = 1500) -> dict[str, Any]:
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
    Current US + Canadian fire perimeters intersecting a lon/lat bounding box, as
    GeoJSON. `offset` is maxAllowableOffset in degrees (0 = full resolution) for the
    US layer; the caller sets it to about a pixel's worth so a zoomed-in view stays
    crisp while the payload stays small. Canadian footprints are clipped to the bbox
    client-side and are best-effort.
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

    async def _us():
        async with httpx.AsyncClient(timeout=45.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
            resp = await client.get(WFIGS_PERIMS_URL, params=params)
            resp.raise_for_status()
            return resp.json()

    us, ca = await asyncio.gather(
        _us(),
        _ca_perimeters(min_acres=min_acres, bbox=(west, south, east, north)),
        return_exceptions=True,
    )
    return _merge_perimeter_fcs(us, ca)


def _merge_perimeter_fcs(us: Any, ca: Any) -> dict[str, Any]:
    """Combine US + Canadian perimeter results, tolerating a failure of either."""
    features: list[dict[str, Any]] = []
    if isinstance(us, dict):
        features.extend(us.get("features") or [])
    if isinstance(ca, dict):
        features.extend(ca.get("features") or [])
    # If both failed, surface the US error so the router returns a clear 502.
    if not isinstance(us, dict) and not isinstance(ca, dict):
        if isinstance(us, BaseException):
            raise us
    return {"type": "FeatureCollection", "features": features}


async def _ca_perimeters(
    min_acres: float = 100.0,
    bbox: Optional[tuple[float, float, float, float]] = None,
) -> dict[str, Any]:
    """
    Canadian Fire M3 perimeter estimates as GeoJSON, normalised to the same
    properties the US perimeters use (poly_IncidentName, poly_GISAcres) so the map
    styles them identically. Filtered by size and, when given, clipped to a bbox.
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
        if bbox is not None and not _geom_intersects_bbox(geom, bbox):
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


def _geom_intersects_bbox(geom: dict[str, Any], bbox: tuple[float, float, float, float]) -> bool:
    """Cheap bbox-vs-geometry-bounds overlap test (no shapely needed)."""
    west, south, east, north = bbox
    xs: list[float] = []
    ys: list[float] = []

    def _walk(c):
        if isinstance(c, (int, float)):
            return
        if c and isinstance(c[0], (int, float)) and len(c) >= 2:
            xs.append(c[0]); ys.append(c[1]); return
        for sub in c:
            _walk(sub)

    _walk(geom.get("coordinates") or [])
    if not xs:
        return False
    return not (max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north)


async def nearest_perimeter_geometry(
    lat: float, lon: float, radius_km: float = 10.0, offset: float = 0.0
) -> Optional[dict[str, Any]]:
    """
    Return the perimeter geometry belonging to the clicked fire, for ignition.

    Among the mapped perimeters near the point we pick the one that CONTAINS the
    point (a fire's incident location sits within its own footprint); if none
    contains it, we pick the genuinely closest. This avoids seeding the forecast
    from a neighbouring fire's perimeter when several are nearby — which makes the
    forecast start from the wrong shape.
    """
    from shapely.geometry import Point, shape

    # Fire could be in the US (WFIGS) or Canada (CWFIS M3) — check both near the
    # point. A small bbox around the point clips the Canadian layer cheaply.
    d = radius_km / 111.0
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        us_fc, ca_fc = await asyncio.gather(
            _fetch_perimeters(client, lat, lon, radius_km, offset=offset),
            _ca_perimeters(min_acres=0.0, bbox=(lon - d, lat - d, lon + d, lat + d)),
            return_exceptions=True,
        )
    feats = []
    if isinstance(us_fc, dict):
        feats.extend(us_fc.get("features") or [])
    if isinstance(ca_fc, dict):
        feats.extend(ca_fc.get("features") or [])
    if not feats:
        return None

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
            return geom                     # point inside → this is the fire's own perimeter
        d = pt.distance(poly)               # planar degrees; fine for ranking nearby polys
        if d < closest_dist:
            closest_dist, closest_geom = d, geom
    return closest_geom or feats[0].get("geometry")
