"""
Evacuation route planning.

Given the user's location and the danger area (the fire's current perimeter plus,
if a forecast was run, its predicted spread), produce a few *traffic-aware* driving
routes that lead AWAY from the fire to a safe destination.

Two things make this wildfire-specific rather than a generic "get directions":

  1. Destinations are chosen to be genuinely safe — outside the danger area and,
     where possible, on the far side of it from the user (so you don't drive toward
     the fire). We use a HYBRID of real designated shelters and computed safe places:
       a. FEMA National Shelter System — currently OPEN shelters (real, but only
          populated during declared incidents, so often empty).
       b. OpenStreetMap — official assembly points, hospitals, community centres,
          and nearby towns/cities, near but clear of the fire.
       c. A geometric fallback — points a safe distance away in the "away from
          fire" directions, snapped to the nearest real town — so we ALWAYS have
          somewhere to route to even when a+b are empty.

  2. Every candidate route is checked against the danger polygon and any route that
     passes through the fire / forecast spread is dropped (Mapbox can't avoid an
     arbitrary polygon, so we request alternatives and filter). What survives is
     ranked by live-traffic ETA.

Traffic-aware routing uses the Mapbox Directions API `driving-traffic` profile
(needs MAPBOX_TOKEN). Without a token we still return the safe destinations and a
note explaining routing is disabled.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from ..config import get_settings
from . import geocoding
from .geo import (
    angle_diff_deg,
    haversine_km,
    initial_bearing_deg,
    point_at_distance_bearing,
)

# How far outside the fire a destination must sit to count as "safe" (km).
_SAFE_BUFFER_KM = 12.0
# A route is flagged "passes near fire" only if it runs through the danger area
# for more than this, IGNORING the first stretch near the origin — the user may
# be standing inside/next to the fire, so every route unavoidably starts there.
_ORIGIN_IGNORE_KM = 1.5
_DANGER_HIT_KM = 0.75
# Search radius for real shelters / POIs around the user (km).
_POI_SEARCH_KM = 60.0
# How many distinct destinations to actually request routes to.
_MAX_DESTINATIONS = 6
# Two chosen routes whose destinations are within this of EACH OTHER lead to the
# same place/corridor, so showing both wastes a slot. The few routes we display are
# spread out to at least this separation (a town and an assembly point beside it
# collapse to one), so the options head in genuinely different directions.
_CORRIDOR_KM = 10.0

_MAPBOX_DIRECTIONS = "https://api.mapbox.com/directions/v5/mapbox/driving-traffic"
_MAPBOX_GEOCODE = "https://api.mapbox.com/geocoding/v5/mapbox.places"
# Keyless fallback router (public OSRM demo server). No live traffic and no API
# key, but it returns real road geometry so drive routes still render when no
# MAPBOX_TOKEN is configured.
_OSRM_DIRECTIONS = "https://router.project-osrm.org/route/v1/driving"
# FEMA National Shelter System — the "Open Shelters" layer: shelters that are
# ACTUALLY OPEN right now. FEMA syncs it from the American Red Cross shelter
# database every morning and re-checks every 20 minutes, so during a real
# evacuation the Red Cross-opened shelters show up here. (There is also a 71k-row
# "designated facilities" table, but it is a non-spatial table of buildings that
# *could* be shelters, not ones that are open — so we don't use it.)
_FEMA_OPEN_SHELTERS = (
    "https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/FeatureServer/0/query"
)
_OVERPASS = "https://overpass-api.de/api/interpreter"

_UA = {"User-Agent": "WildfireMap/0.1"}


# --------------------------------------------------------------------------- #
# Danger geometry
# --------------------------------------------------------------------------- #

def _danger_union(avoid_geojson: Optional[dict], fallback_geom: Optional[dict]):
    """
    Build one shapely (multi)polygon representing everything to avoid, from a
    GeoJSON geometry / Feature / FeatureCollection. Returns (geom, centroid_lonlat)
    or (None, None).
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    polys = []
    for src in (avoid_geojson, fallback_geom):
        for geom in _iter_geometries(src):
            try:
                g = shape(geom)
                if not g.is_valid:
                    g = g.buffer(0)
                if not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon"):
                    polys.append(g)
            except Exception:
                continue
        if polys:                       # prefer the explicit avoid set if it worked
            break
    if not polys:
        return None, None
    merged = unary_union(polys)
    c = merged.centroid
    return merged, (c.x, c.y)


def _geom_length_km(geom) -> float:
    """
    Total length in km of the line parts of a geometry, summing haversine over
    segments. Robust to a GeometryCollection (line ∩ polygon can mix in points /
    polygons) — only LineString parts contribute.
    """
    if geom is None or geom.is_empty:
        return 0.0
    if geom.geom_type in ("MultiLineString", "GeometryCollection", "MultiPolygon", "MultiPoint"):
        return sum(_geom_length_km(g) for g in geom.geoms)
    if geom.geom_type != "LineString":
        return 0.0
    coords = list(geom.coords)
    return sum(
        haversine_km(y1, x1, y2, x2)
        for (x1, y1), (x2, y2) in zip(coords, coords[1:])
    )


def _iter_geometries(src: Optional[dict]):
    if not src:
        return
    t = src.get("type")
    if t == "FeatureCollection":
        for f in src.get("features", []):
            g = f.get("geometry")
            if g:
                yield g
    elif t == "Feature":
        g = src.get("geometry")
        if g:
            yield g
    elif t:
        yield src


# --------------------------------------------------------------------------- #
# Candidate safe destinations (hybrid)
# --------------------------------------------------------------------------- #

async def _fema_open_shelters(client: httpx.AsyncClient, lat: float, lon: float) -> list[dict]:
    """Currently-OPEN Red Cross/FEMA shelters within the search radius."""
    params = {
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "distance": str(_POI_SEARCH_KM * 1000),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "shelter_name,org_name,address,city,state,evacuation_capacity",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    out: list[dict] = []
    try:
        r = await client.get(_FEMA_OPEN_SHELTERS, params=params, timeout=20.0)
        r.raise_for_status()
        for feat in (r.json().get("features") or []):
            g = feat.get("geometry") or {}
            c = g.get("coordinates") or [None, None]
            if c[0] is None:
                continue
            p = feat.get("properties", {})
            cap = p.get("evacuation_capacity")
            address = ", ".join(x for x in (p.get("address"), p.get("city"), p.get("state")) if x) or None
            detail = address or ""
            if cap:
                detail = (detail + f" · capacity {cap}").lstrip(" ·")
            out.append({
                "name": (p.get("shelter_name") or p.get("org_name") or "Open shelter").strip(),
                "detail": detail or "open shelter",
                "address": address,
                "lat": c[1], "lon": c[0], "category": "shelter", "source": "FEMA",
            })
    except Exception:
        pass
    return out


async def _osm_safe_pois(client: httpx.AsyncClient, lat: float, lon: float) -> list[dict]:
    radius_m = int(_POI_SEARCH_KM * 1000)
    # Note: OSM's `amenity=shelter` is mostly picnic/transit shelters, so we skip
    # it and rely on FEMA for real emergency shelters. `emergency=assembly_point`
    # IS a genuine designated evacuation muster point; hospitals and community
    # centres are dependable "safe area" fallbacks; and towns/cities give a real,
    # named place to head for when nothing else is nearby.
    q = f"""
    [out:json][timeout:25];
    (
      node[emergency=assembly_point](around:{radius_m},{lat},{lon});
      node[amenity=hospital](around:{radius_m},{lat},{lon});
      node[amenity=community_centre](around:{radius_m},{lat},{lon});
      node[place~"^(city|town|village)$"](around:{radius_m},{lat},{lon});
    );
    out center 120;
    """
    out: list[dict] = []
    try:
        r = await client.post(_OVERPASS, data={"data": q}, timeout=25.0)
        r.raise_for_status()
        for el in (r.json().get("elements") or []):
            plat = el.get("lat") or (el.get("center") or {}).get("lat")
            plon = el.get("lon") or (el.get("center") or {}).get("lon")
            if plat is None or plon is None:
                continue
            tags = el.get("tags", {})
            place = tags.get("place")
            amen = tags.get("amenity") or tags.get("emergency") or ("town" if place else "place")
            # A street address from OSM tags when the feature carries them (many
            # assembly points / community centres do); falls back to city/state.
            road = " ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street")) if x)
            address = ", ".join(x for x in (road, tags.get("addr:city"), tags.get("addr:state")) if x) or None
            detail = address or ""
            if not detail and place:
                detail = place.replace("_", " ").title()          # e.g. "Town", "Village"
            out.append({
                "name": tags.get("name") or amen.replace("_", " ").title(),
                "detail": detail,
                "address": address,
                "lat": plat, "lon": plon,
                "category": "shelter" if amen == "assembly_point" else "safe_area",
                "source": "OSM",
            })
    except Exception:
        pass
    return out


def _geometric_safe_points(lat: float, lon: float, fire_lonlat, away_bearing: float) -> list[dict]:
    """
    Always-available fallback: points a safe distance out, fanned around the
    away-from-fire direction. Not real places, but guaranteed to give the router
    somewhere clear to aim for. Labelled generically; the frontend reverse-geocodes.
    """
    pts = []
    for spread in (-45, 0, 45):
        for dist in (_SAFE_BUFFER_KM + 8, _SAFE_BUFFER_KM + 25):
            dlon, dlat = point_at_distance_bearing(lat, lon, dist, (away_bearing + spread) % 360)
            pts.append({
                "name": "Safe area", "detail": "away from the fire",
                "lat": dlat, "lon": dlon, "category": "safe_area", "source": "computed",
            })
    return pts


async def _snap_to_nearest_town(client: httpx.AsyncClient, token: str, pt: dict) -> dict:
    """
    Replace a synthetic geometric safe-point with the nearest real populated
    place (Mapbox reverse geocoding, `place` type) so even the fallback aims at a
    town with a name, not an empty patch of map. Falls back to the original point.
    """
    url = f"{_MAPBOX_GEOCODE}/{pt['lon']},{pt['lat']}.json"
    try:
        r = await client.get(url, params={"types": "place", "limit": "1", "access_token": token}, timeout=15.0)
        r.raise_for_status()
        feats = r.json().get("features") or []
        if feats:
            f = feats[0]
            lon, lat = f["center"]
            return {**pt, "name": f.get("text") or pt["name"],
                    "detail": "nearest town", "lat": lat, "lon": lon,
                    "category": "safe_area", "source": "Mapbox"}
    except Exception:
        pass
    return pt


def _filter_and_rank_destinations(
    dests: list[dict], user_lat: float, user_lon: float, fire_lonlat, danger, away_bearing: float
) -> list[dict]:
    """
    Keep destinations that are actually safe (clear of the danger area + a real
    distance from the fire) and rank them: prefer the far side of the fire from the
    user, reasonably close, real shelters ahead of computed points.
    """
    from shapely.geometry import Point

    f_lon, f_lat = fire_lonlat
    scored = []
    for d in dests:
        # Never send someone to a point inside the fire / forecast spread.
        if danger is not None:
            try:
                if danger.contains(Point(d["lon"], d["lat"])):
                    continue
            except Exception:
                pass
        km_from_fire = haversine_km(f_lat, f_lon, d["lat"], d["lon"])
        if km_from_fire < _SAFE_BUFFER_KM:
            continue
        km_from_user = haversine_km(user_lat, user_lon, d["lat"], d["lon"])
        if km_from_user < 1.0 or km_from_user > _POI_SEARCH_KM + 30:
            continue
        # Bearing from user to destination — reward heading away from the fire.
        brg = initial_bearing_deg(user_lat, user_lon, d["lat"], d["lon"])
        away_penalty = angle_diff_deg(brg, away_bearing) / 180.0        # 0 good, 1 bad
        # Destination preference (lower = better). An OPEN official shelter is run
        # for this incident (capacity, services), so prefer it most; then designated
        # assembly points (also category "shelter") over a bare town; synthetic/
        # computed points last.
        if d["source"] == "FEMA":
            pref = -0.25
        elif d["category"] == "shelter":
            pref = -0.2
        elif d["source"] == "computed":
            pref = 0.15
        else:
            pref = 0.0
        # Score: lower is better. Weight away-direction heaviest, then closeness.
        score = away_penalty * 1.0 + (km_from_user / 100.0) + pref
        d = {**d, "km_from_user": round(km_from_user, 1), "km_from_fire": round(km_from_fire, 1),
             "bearing": round(brg), "_score": score}
        scored.append(d)
    scored.sort(key=lambda x: x["_score"])
    # De-dup near-identical destinations (within ~1.5 km of an already-picked one).
    picked: list[dict] = []
    for d in scored:
        if all(haversine_km(d["lat"], d["lon"], p["lat"], p["lon"]) > 1.5 for p in picked):
            picked.append(d)
        if len(picked) >= _MAX_DESTINATIONS:
            break
    return picked


# --------------------------------------------------------------------------- #
# Traffic-aware routing (Mapbox) + danger filtering
# --------------------------------------------------------------------------- #

def _origin_ignore_region(user_lat: float, user_lon: float, danger):
    """
    The stretch of any route near the origin that we ignore when deciding whether
    it "passes through" the fire. If the user is INSIDE the fire, getting out is
    unavoidable, so the ignored radius grows with how deep they are (distance to
    the fire edge); if they're outside, we only tolerate a small clip.
    """
    from shapely.geometry import Point

    if danger is None:
        return None
    op = Point(user_lon, user_lat)
    depth_km = op.distance(danger.boundary) * 111.0 if danger.contains(op) else 0.0
    return op.buffer((_ORIGIN_IGNORE_KM + depth_km) / 111.0)


def _route_passes_danger(geom: dict, danger, origin_ignore) -> bool:
    """True if a route runs through the danger area for more than _DANGER_HIT_KM,
    ignoring the unavoidable stretch near the origin."""
    if danger is None:
        return False
    from shapely.geometry import shape as shp

    try:
        inside = shp(geom).intersection(danger)
        if not inside.is_empty and origin_ignore is not None:
            inside = inside.difference(origin_ignore)
        return _geom_length_km(inside) > _DANGER_HIT_KM
    except Exception:
        return False


def _route_record(dest: dict, geom: dict, distance_m: float, duration_s: float,
                  duration_typical_s, passes: bool) -> dict:
    return {
        "destination": {**{k: dest[k] for k in ("name", "detail", "lat", "lon", "category", "source")},
                        "address": dest.get("address")},
        "geometry": geom,
        "distance_km": round((distance_m or 0) / 1000.0, 1),
        "duration_min": round((duration_s or 0) / 60.0, 1),
        "duration_typical_min": round(duration_typical_s / 60.0, 1) if duration_typical_s else None,
        "passes_near_fire": passes,
        "km_from_fire": dest.get("km_from_fire"),
    }


async def _mapbox_routes(
    client: httpx.AsyncClient, token: str, user_lat: float, user_lon: float, dest: dict, danger
) -> list[dict]:
    origin_ignore = _origin_ignore_region(user_lat, user_lon, danger)
    url = f"{_MAPBOX_DIRECTIONS}/{user_lon},{user_lat};{dest['lon']},{dest['lat']}"
    params = {
        "alternatives": "true",
        "geometries": "geojson",
        "overview": "full",
        "annotations": "duration,congestion",
        "access_token": token,
    }
    try:
        r = await client.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    routes = []
    for rt in data.get("routes", []):
        geom = rt.get("geometry")
        if not geom:
            continue
        routes.append(_route_record(
            dest, geom, rt.get("distance", 0), rt.get("duration", 0),
            rt.get("duration_typical"), _route_passes_danger(geom, danger, origin_ignore),
        ))
    return routes


async def _osrm_routes(
    client: httpx.AsyncClient, user_lat: float, user_lon: float, dest: dict, danger
) -> list[dict]:
    """Keyless driving routes via the public OSRM server (no traffic). Same danger
    filtering as the Mapbox path so contained routes are still flagged."""
    origin_ignore = _origin_ignore_region(user_lat, user_lon, danger)
    url = f"{_OSRM_DIRECTIONS}/{user_lon},{user_lat};{dest['lon']},{dest['lat']}"
    params = {"alternatives": "true", "overview": "full", "geometries": "geojson"}
    try:
        r = await client.get(url, params=params, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []
    if data.get("code") != "Ok":
        return []
    routes = []
    for rt in data.get("routes", []):
        geom = rt.get("geometry")
        if not geom:
            continue
        routes.append(_route_record(
            dest, geom, rt.get("distance", 0), rt.get("duration", 0),
            None, _route_passes_danger(geom, danger, origin_ignore),
        ))
    return routes


async def plan(
    lat: float,
    lon: float,
    fire_lat: Optional[float],
    fire_lon: Optional[float],
    avoid_geojson: Optional[dict],
    max_routes: int,
    country: Optional[str] = None,
) -> dict[str, Any]:
    settings = get_settings()
    notes: list[str] = []
    # The FEMA National Shelter System is a US-only feed, so in Canada it's skipped
    # entirely — Canadian evacuees are directed to Red Cross reception centres and
    # provincial/territorial emergency management, which have no equivalent free
    # national real-time API, so destinations there come from OSM (assembly points,
    # community centres, hospitals, towns) plus the geometric fallback.
    is_canada = (country or "").upper() == "CA"

    # 1. Danger geometry. Use the caller's avoid set (perimeter + forecast) if given;
    #    otherwise pull the fire's mapped perimeter so we still avoid it. Look for
    #    the perimeter at the FIRE's location (it can be tens of km from the user),
    #    not near the user — otherwise a distant fire yields no danger area.
    fallback_geom = None
    if not avoid_geojson:
        from . import fires as fires_svc
        perim_lat = fire_lat if fire_lat is not None else lat
        perim_lon = fire_lon if fire_lon is not None else lon
        # The WFIGS perimeter service is occasionally slow; retry once so a single
        # timeout doesn't silently drop the fire's footprint (which matters most
        # for a nearby fire, where routes must actually avoid it).
        for _attempt in range(2):
            try:
                # ~500 m simplification: a big fire's full-res perimeter is slow to
                # transfer; a simplified footprint is plenty to route around.
                fallback_geom = await fires_svc.nearest_perimeter_geometry(
                    perim_lat, perim_lon, offset=0.005
                )
                if fallback_geom:
                    break
            except Exception:
                fallback_geom = None
    danger, danger_centroid = _danger_union(avoid_geojson, fallback_geom)

    # Fire reference point for the "away" direction.
    if fire_lat is not None and fire_lon is not None:
        fire_lonlat = (fire_lon, fire_lat)
    elif danger_centroid is not None:
        fire_lonlat = danger_centroid
    else:
        fire_lonlat = (lon, lat)
    if danger is None:
        notes.append("No mapped fire area available — routes avoid the fire's location only.")

    # Direction pointing FROM the fire, through the user, onward to safety.
    away_bearing = initial_bearing_deg(fire_lonlat[1], fire_lonlat[0], lat, lon)

    # 2. Candidate destinations (hybrid: real shelters + OSM + geometric fallback).
    #    When a Mapbox token exists, snap the synthetic fallback points to the
    #    nearest real town so they aim somewhere meaningful.
    token = settings.mapbox_token
    geo_pts = _geometric_safe_points(lat, lon, fire_lonlat, away_bearing)
    async with httpx.AsyncClient(headers=_UA) as client:
        # FEMA is US-only — in Canada we substitute an empty result and lean on OSM.
        shelter_task = (
            asyncio.sleep(0, result=[]) if is_canada
            else _fema_open_shelters(client, lat, lon)
        )
        tasks = [shelter_task, _osm_safe_pois(client, lat, lon)]
        if token:
            tasks += [_snap_to_nearest_town(client, token, p) for p in geo_pts]
        results = await asyncio.gather(*tasks)
    shelters, pois = results[0], results[1]
    if token:
        geo_pts = list(results[2:])
    real = shelters + pois
    candidates = list(real) + geo_pts
    dests = _filter_and_rank_destinations(candidates, lat, lon, fire_lonlat, danger, away_bearing)
    # Give every shown destination a street address. Real shelters / OSM POIs may
    # already carry one from their tags; the rest (assembly points without address
    # tags, computed safe points, snapped towns) are reverse-geocoded here — SEQUENTIALLY
    # (Nominatim's policy is ~1 request/second, so we don't fire a parallel burst) and
    # under a total time budget so a slow geocoder can't stall the evacuation response.
    missing = [d for d in dests if not d.get("address")]
    if missing:
        async def _fill_addresses():
            async with httpx.AsyncClient(timeout=10.0, headers=_UA) as client:
                for d in missing:
                    a = await geocoding.reverse(d["lat"], d["lon"], client)
                    if a:
                        d["address"] = a
        try:
            await asyncio.wait_for(_fill_addresses(), timeout=12.0)
        except asyncio.TimeoutError:
            pass   # best-effort: whatever addresses we got so far still stand
    if not any(d["category"] == "shelter" for d in dests):
        notes.append("No open shelters listed near you — routing to the safest nearby town/facility instead.")
    # The official evacuation/reception centre for THIS fire is announced per-incident
    # and won't appear in our feeds, so always point to the authoritative source — which
    # differs by country (there's no Watch Duty / FEMA equivalent in Canada).
    if is_canada:
        notes.append("Confirm the official reception centre and evacuation order with your "
                     "provincial/territorial emergency management or the Canadian Red Cross "
                     "(1-800-863-6582) — it may differ from the destinations shown.")
    else:
        notes.append("Confirm the official evacuation centre and current order via Watch Duty "
                     "or your county emergency management — it may differ from the destinations shown.")

    # 3. Driving routes to each destination, then drop any that cross the fire.
    #    Prefer Mapbox (live traffic) when a token exists; otherwise fall back to
    #    the keyless OSRM server so routes still render — just without traffic.
    async with httpx.AsyncClient(headers=_UA) as client:
        if token:
            route_groups = await asyncio.gather(
                *[_mapbox_routes(client, token, lat, lon, d, danger) for d in dests]
            )
        else:
            notes.append(
                "Routes use keyless routing (no live traffic). Set MAPBOX_TOKEN for "
                "traffic-aware ETAs."
            )
            route_groups = await asyncio.gather(
                *[_osrm_routes(client, lat, lon, d, danger) for d in dests]
            )
    all_routes = [r for group in route_groups for r in group]

    # One best route per destination, clear routes first, then by live ETA.
    best_by_dest: dict[tuple, dict] = {}
    for r in all_routes:
        key = (round(r["destination"]["lat"], 4), round(r["destination"]["lon"], 4))
        cur = best_by_dest.get(key)
        better = (
            cur is None
            or (not r["passes_near_fire"] and cur["passes_near_fire"])
            or (r["passes_near_fire"] == cur["passes_near_fire"] and r["duration_min"] < cur["duration_min"])
        )
        if better:
            best_by_dest[key] = r
    # Rank clear routes first, then by drive time — but treat an official shelter /
    # designated assembly point as if it were several minutes closer, so a purpose-
    # designated evacuation destination wins over a bare town centroid when the drive
    # times are comparable (it only loses when a town is meaningfully closer).
    def _pref_minutes(r):
        dest = r["destination"]
        if dest["source"] == "FEMA":
            return 8.0
        if dest["category"] == "shelter":
            return 6.0
        return 0.0
    ranked = sorted(
        best_by_dest.values(),
        key=lambda r: (r["passes_near_fire"], r["duration_min"] - _pref_minutes(r)),
    )
    # Pick for directional DIVERSITY: skip a route whose destination sits in the same
    # corridor as one already chosen (within _CORRIDOR_KM of it) — a town and the
    # assembly point beside it collapse to one, keeping the better-ranked, so the
    # options we show head to genuinely different places. If distinct corridors run
    # out, top up with the next-best routes so we still offer up to max_routes.
    def _corridor_clear(cand, chosen):
        cd = cand["destination"]
        return all(
            haversine_km(cd["lat"], cd["lon"], c["destination"]["lat"], c["destination"]["lon"]) >= _CORRIDOR_KM
            for c in chosen
        )
    routes, deferred = [], []
    for r in ranked:
        if len(routes) < max_routes and _corridor_clear(r, routes):
            routes.append(r)
        else:
            deferred.append(r)
    for r in deferred:                          # backfill only if we're short on distinct corridors
        if len(routes) >= max_routes:
            break
        routes.append(r)
    for i, r in enumerate(routes):
        r["recommended"] = (i == 0 and not r["passes_near_fire"])
    if routes and all(r["passes_near_fire"] for r in routes):
        notes.append("Every available road passes near the fire — leave early and drive with caution.")
    elif not routes:
        notes.append("No drivable route found to a safe area. Follow local emergency instructions.")

    return {
        "origin": {"lat": lat, "lon": lon},
        "away_bearing": round(away_bearing),
        "routes": routes,
        "destinations": [
            {**{k: d[k] for k in ("name", "detail", "lat", "lon", "category", "source",
                                  "km_from_user", "km_from_fire", "bearing")},
             "address": d.get("address")}
            for d in dests
        ],
        "notes": notes,
    }
