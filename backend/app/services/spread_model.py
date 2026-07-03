"""
Built-in wildfire spread model — produces the forecast isochrones the map draws.

This is a documented, defensible *elliptical* fire-growth model, the same family
of simplification used operationally before/around full 2D simulators:

  * Head rate-of-spread scales a fuel-specific reference ROS by wind and slope
    (a Rothermel-inspired multiplicative form).
  * The fire front is modeled as an ellipse whose elongation (length-to-breadth)
    grows with wind speed, per Alexander (1985) / Anderson (1983).
  * The ignition point sits at the rear focus; the ellipse is oriented in the
    direction the wind blows toward.

It is intentionally simple and runs in milliseconds with no external engine, so
the "predicted movement" feature works out of the box. When ForeFire is installed
the adapter uses that instead (see forefire_adapter.py); this stays as the fallback
and as a sanity baseline. It is a research/education tool, NOT operational fire
behavior guidance.

References:
  Rothermel, R.C. (1972). A mathematical model for predicting fire spread.
  Anderson, H.E. (1983). Predicting wind-driven wildland fire size and shape.
  Alexander, M.E. (1985). Estimating the length-to-breadth ratio of elliptical
    fire patterns.
  Scott & Burgan (2005). Standard fire behavior fuel models.
"""
import math
from typing import Any, Optional

from .geo import (
    bearing_to_math_radians,
    local_meters_to_lonlat,
    lonlat_to_local_meters,
    wind_from_to_toward_bearing,
)

# Wind speed (km/h) the fuel table's ros_ref is calibrated to.
REF_WIND_KMH = 20.0
# Calibration constant tying the wind multiplier to the reference ROS (see notes
# in the module docstring / DATA_SOURCES.md).
WIND_K = 11.0
ELLIPSE_POINTS = 72


def head_ros_m_per_min(ros_ref: float, wind_factor: float, wind_kmh: float, slope_percent: float) -> float:
    """
    Head (fastest, downwind) rate of spread in meters/minute.

        R0     = no-wind baseline (a small fraction of the reference ROS)
        phi_w  = wind multiplier, rising with wind^1.5 and the fuel's wind response
        phi_s  = slope multiplier, rising with steepness
        R_head = R0 * (1 + phi_w + phi_s)
    """
    r0 = 0.1 * ros_ref
    phi_w = wind_factor * (max(0.0, wind_kmh) ** 1.5) / WIND_K
    slope_frac = max(0.0, slope_percent) / 100.0
    phi_s = 2.0 * (slope_frac ** 1.3)
    return r0 * (1.0 + phi_w + phi_s)


def length_to_breadth(wind_kmh: float) -> float:
    """
    Ellipse length-to-breadth ratio as a function of wind speed (Alexander 1985
    form, evaluated with wind in m/s). Bounded to [1, 8]: 1 = calm circle.
    """
    u_ms = wind_kmh / 3.6
    lb = 0.936 * math.exp(0.2566 * u_ms) + 0.461 * math.exp(-0.1548 * u_ms) - 0.397
    return max(1.0, min(8.0, lb))


def _ellipse_ring(
    origin_lat: float,
    origin_lon: float,
    head_distance_m: float,
    lb: float,
    toward_bearing_deg: float,
) -> list[list[float]]:
    """
    Build one closed GeoJSON ring (list of [lon,lat]) for an elliptical front.

    Geometry: the ignition point is the rear focus. With eccentricity e and
    semi-major a, the head distance (focus to far tip) is a*(1+e). The ellipse
    center sits at c = a*e downwind of the ignition point.
    """
    e = math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb)))
    a = head_distance_m / (1.0 + e)     # semi-major
    b = a / lb                          # semi-minor
    c = a * e                           # focus offset (center is c downwind)

    theta = bearing_to_math_radians(toward_bearing_deg)  # major-axis direction, math frame
    major = (math.cos(theta), math.sin(theta))           # (east, north)
    perp = (-math.sin(theta), math.cos(theta))

    ring: list[list[float]] = []
    for i in range(ELLIPSE_POINTS + 1):
        t = 2.0 * math.pi * i / ELLIPSE_POINTS
        # point relative to ignition origin, in meters (east, north)
        along = c + a * math.cos(t)
        across = b * math.sin(t)
        dx = along * major[0] + across * perp[0]
        dy = along * major[1] + across * perp[1]
        lon, lat = local_meters_to_lonlat(origin_lat, origin_lon, dx, dy)
        ring.append([lon, lat])
    return ring


def _ellipse_area_km2(head_distance_m: float, lb: float) -> float:
    e = math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb)))
    a = head_distance_m / (1.0 + e)
    b = a / lb
    return math.pi * a * b / 1_000_000.0


def simulate(
    lat: float,
    lon: float,
    duration_hours: float,
    step_minutes: int,
    wind_speed_kmh: float,
    wind_direction_deg: float,
    ros_ref: float,
    wind_factor: float,
    slope_percent: float,
) -> dict[str, Any]:
    """
    Run the elliptical model and return a GeoJSON FeatureCollection with one
    polygon per time step (nested isochrones), each tagged with the elapsed time,
    head-spread distance, and burned area.

    wind_direction_deg is the meteorological FROM direction; the fire is pushed
    the opposite way.
    """
    toward = wind_from_to_toward_bearing(wind_direction_deg)
    lb = length_to_breadth(wind_speed_kmh)
    ros = head_ros_m_per_min(ros_ref, wind_factor, wind_speed_kmh, slope_percent)

    features: list[dict[str, Any]] = []
    steps = max(1, int(round(duration_hours * 60 / step_minutes)))
    for s in range(1, steps + 1):
        minutes = s * step_minutes
        head_m = ros * minutes
        if head_m <= 0:
            continue
        ring = _ellipse_ring(lat, lon, head_m, lb, toward)
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "step": s,
                "minutes": minutes,
                "hours": round(minutes / 60.0, 2),
                "head_distance_km": round(head_m / 1000.0, 3),
                "area_km2": round(_ellipse_area_km2(head_m, lb), 3),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "head_ros_m_per_min": round(ros, 3),
            "length_to_breadth": round(lb, 2),
            "wind_toward_bearing_deg": round(toward, 1),
        },
    }


# --- Time-varying propagation (robust anisotropic polygon growth) ------------
#
# The closed-form `simulate` above assumes ONE constant wind. To make a genuinely
# time-evolving forecast that bends as the hourly wind shifts, we grow the fire
# footprint incrementally: at each hourly step we expand the current polygon by an
# ELLIPTICAL structuring element (a Minkowski sum) sized from that hour's wind —
# larger downwind, smaller backing, per the Alexander (1985) length-to-breadth
# relation. This is the geometric equivalent of Huygens-wavelet fire growth
# (Anderson 1983; Richards 1990) used by simulators like FARSITE, but computed
# with robust polygon geometry (shapely). Unlike a vertex-by-vertex scheme it
# never produces radial spikes on jagged real perimeters. Research tool, not
# operational guidance.
#
# Anisotropic-growth trick: rotate the polygon so the wind blows along +x, scale x
# so the target ellipse becomes a circle, buffer by that radius, undo the scale,
# shift downwind by the focus offset, rotate back. Unioning with the previous step
# keeps the isochrones strictly nested for the time slider.

from shapely import affinity
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

FRONT_POINTS = 180           # seed-circle resolution / reference
SEED_RADIUS_M = 40.0         # point ignition starts as a small circle
SIMPLIFY_TOLERANCE_M = 20.0  # keep vertex counts sane between steps


def _ring_area(ring: list[list[float]]) -> float:
    """Absolute shoelace area of a lon/lat ring (in deg^2 — only for comparison)."""
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def _largest_ring(geometry: dict) -> Optional[list[list[float]]]:
    """Extract the largest exterior ring (list of [lon,lat]) from a GeoJSON
    Polygon or MultiPolygon geometry, chosen by area."""
    gtype = (geometry or {}).get("type")
    coords = (geometry or {}).get("coordinates")
    if not coords:
        return None
    if gtype == "Polygon":
        return coords[0]
    if gtype == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
        return max(rings, key=_ring_area) if rings else None
    return None


def _largest_part(geom):
    """Return the largest Polygon part of a (possibly Multi) geometry."""
    if geom.geom_type == "Polygon":
        return geom
    polys = [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    return max(polys, key=lambda g: g.area) if polys else geom


def perimeter_to_polygon(geometry: dict):
    """
    Turn a GeoJSON fire perimeter into (origin_lat, origin_lon, polygon), where
    `polygon` is a shapely Polygon in local meters about the perimeter centroid.
    `buffer(0)` repairs minor self-intersections in the source data. Returns None
    if the geometry can't be used.
    """
    ring = _largest_ring(geometry)
    if not ring or len(ring) < 3:
        return None
    verts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if len(verts) < 3:
        return None

    origin_lon = sum(p[0] for p in verts) / len(verts)
    origin_lat = sum(p[1] for p in verts) / len(verts)
    pts_m = [lonlat_to_local_meters(origin_lat, origin_lon, lon, lat) for lon, lat in verts]

    poly = Polygon(pts_m)
    if not poly.is_valid:
        poly = poly.buffer(0)
    poly = _largest_part(poly)
    if poly.is_empty or poly.area <= 0:
        return None
    return origin_lat, origin_lon, poly


def _grow(poly, head_m: float, lb: float, toward_deg: float):
    """
    Expand `poly` by an elliptical structuring element (Minkowski sum) sized to
    one step's spread: downwind reach `head_m`, elongation `lb`. See module notes.
    """
    e = math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb)))
    a = head_m / (1.0 + e)          # semi-major (downwind reach = a*(1+e) = head_m)
    if a <= 0:
        return poly
    b = a / lb                      # semi-minor (lateral)
    c = a * e                       # focus offset (extra downwind shift)
    theta = 90.0 - toward_deg       # math angle (CCW from +x) of the wind-toward dir
    p = affinity.rotate(poly, -theta, origin=(0, 0), use_radians=False)  # wind -> +x
    p = affinity.scale(p, xfact=b / a, yfact=1.0, origin=(0, 0))         # ellipse -> circle
    p = p.buffer(b, quad_segs=24)                                        # round buffer
    p = affinity.scale(p, xfact=a / b, yfact=1.0, origin=(0, 0))         # circle -> ellipse
    p = affinity.translate(p, xoff=c)                                    # shift downwind
    p = affinity.rotate(p, theta, origin=(0, 0), use_radians=False)      # back to map frame
    return p


def simulate_timevarying(
    lat: float,
    lon: float,
    wind_series: list[tuple[float, float]],
    ros_ref: float,
    wind_factor: float,
    slope_percent: float,
    step_minutes: int,
    initial_polygon=None,
) -> dict[str, Any]:
    """
    Grow the fire step by step under a per-step wind series.

    wind_series: one (wind_speed_kmh, wind_direction_deg_FROM) per step. Its
    length sets how many isochrones are produced.

    initial_polygon: optional starting shapely Polygon in local meters about
    (lat,lon) — e.g. a real NIFC footprint from `perimeter_to_polygon`. When
    omitted, the fire starts from a small seed circle (point ignition).

    Returns a GeoJSON FeatureCollection with one strictly-nested polygon per step.
    """
    if initial_polygon is not None:
        poly = initial_polygon if initial_polygon.is_valid else initial_polygon.buffer(0)
        poly = _largest_part(poly)
    else:
        poly = Point(0, 0).buffer(SEED_RADIUS_M, quad_segs=24)

    features: list[dict[str, Any]] = []
    minutes = 0

    for step_idx, (speed, dir_from) in enumerate(wind_series, start=1):
        minutes += step_minutes
        toward = wind_from_to_toward_bearing(dir_from)
        lb = length_to_breadth(speed)
        head_rate = head_ros_m_per_min(ros_ref, wind_factor, speed, slope_percent)  # m/min
        head_m = head_rate * step_minutes

        grown = _grow(poly, head_m, lb, toward)
        poly = unary_union([poly, grown])      # union keeps steps strictly nested
        if not poly.is_valid:
            poly = poly.buffer(0)
        poly = _largest_part(poly)
        poly = poly.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)

        toward_rad = bearing_to_math_radians(toward)
        wx, wy = math.cos(toward_rad), math.sin(toward_rad)
        ext = list(poly.exterior.coords)
        head_dist = max(px * wx + py * wy for px, py in ext)
        ring = [list(local_meters_to_lonlat(lat, lon, px, py)) for px, py in ext]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "step": step_idx,
                "minutes": minutes,
                "hours": round(minutes / 60.0, 2),
                "head_distance_km": round(head_dist / 1000.0, 3),
                "area_km2": round(poly.area / 1_000_000.0, 3),
                "wind_speed_kmh": round(speed, 1),
                "wind_from_deg": round(dir_from, 1),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "model": "anisotropic-minkowski-timevarying",
            "steps": len(features),
            "seeded_from_perimeter": initial_polygon is not None,
        },
    }
