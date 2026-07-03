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


# --- Time-varying propagation (Huygens elliptical-wavelet approximation) ------
#
# The closed-form `simulate` above assumes ONE constant wind. To make a genuinely
# time-evolving forecast that bends as the hourly wind shifts, we instead grow the
# fire perimeter incrementally: at each step every point on the front advances
# outward by that hour's local spread rate. The rate follows the elliptical polar
# form (fastest downwind, slowest backing), which is the basis of Huygens-wavelet
# fire growth (Anderson 1983; Richards 1990) used by simulators like FARSITE.
#
# Simplification: the outward direction at each vertex is taken from the fire's
# centroid (stable and self-intersection-resistant for the convex-ish fronts a
# short forecast produces), rather than the exact local front normal. Good for a
# research prototype; not operational guidance.

FRONT_POINTS = 180
SEED_RADIUS_M = 30.0


def _seed_front(n: int, radius_m: float) -> list[tuple[float, float]]:
    return [
        (radius_m * math.cos(2 * math.pi * k / n), radius_m * math.sin(2 * math.pi * k / n))
        for k in range(n)
    ]


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


def perimeter_to_front(geometry: dict, n_points: int = FRONT_POINTS):
    """
    Turn a GeoJSON fire perimeter into an initial front for propagation.

    Returns (origin_lat, origin_lon, front_pts_meters) where the origin is the
    perimeter centroid and the front is a star-shaped resampling to n_points
    equally-spaced angles about that centroid. Star resampling both normalizes
    vertex count and keeps the front compatible with the centroid-normal
    propagator. Returns None if the geometry can't be used.
    """
    ring = _largest_ring(geometry)
    if not ring or len(ring) < 3:
        return None

    # Drop the closing duplicate vertex so it doesn't skew the centroid.
    verts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    if len(verts) < 3:
        return None

    origin_lon = sum(p[0] for p in verts) / len(verts)
    origin_lat = sum(p[1] for p in verts) / len(verts)

    # Vertices as (angle from centroid, radius in meters).
    polar: list[tuple[float, float]] = []
    for lon, lat in verts:
        dx, dy = lonlat_to_local_meters(origin_lat, origin_lon, lon, lat)
        r = math.hypot(dx, dy)
        if r <= 0:
            continue
        polar.append((math.atan2(dy, dx) % (2 * math.pi), r))
    if len(polar) < 3:
        return None
    polar.sort()

    # Resample: for each target angle take the radius of the nearest source
    # vertex by angle (robust for irregular, roughly star-shaped perimeters).
    angles = [a for a, _ in polar]
    radii = [r for _, r in polar]
    front: list[tuple[float, float]] = []
    for k in range(n_points):
        target = 2 * math.pi * k / n_points
        j = min(range(len(angles)), key=lambda i: abs(((angles[i] - target + math.pi) % (2 * math.pi)) - math.pi))
        r = radii[j]
        front.append((r * math.cos(target), r * math.sin(target)))
    return origin_lat, origin_lon, front


def _centroid(pts: list[tuple[float, float]]) -> tuple[float, float]:
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n)


def _polygon_area_m2(pts: list[tuple[float, float]]) -> float:
    """Shoelace area of a ring given in meters."""
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def simulate_timevarying(
    lat: float,
    lon: float,
    wind_series: list[tuple[float, float]],
    ros_ref: float,
    wind_factor: float,
    slope_percent: float,
    step_minutes: int,
    initial_front: Optional[list[tuple[float, float]]] = None,
) -> dict[str, Any]:
    """
    Grow the fire step by step under a per-step wind series.

    wind_series: one (wind_speed_kmh, wind_direction_deg_FROM) per step. Its
    length sets how many isochrones are produced.

    initial_front: optional starting perimeter as (east,north) meters relative to
    (lat,lon) — e.g. a real NIFC fire footprint from `perimeter_to_front`. When
    omitted, the fire starts from a small seed circle (point ignition).

    Returns the same GeoJSON FeatureCollection shape as `simulate` (one nested
    polygon per step) so the mobile app renders both engines identically.
    """
    front = list(initial_front) if initial_front else _seed_front(FRONT_POINTS, SEED_RADIUS_M)
    features: list[dict[str, Any]] = []
    minutes = 0

    for step_idx, (speed, dir_from) in enumerate(wind_series, start=1):
        minutes += step_minutes
        toward = wind_from_to_toward_bearing(dir_from)
        toward_rad = bearing_to_math_radians(toward)
        wx, wy = math.cos(toward_rad), math.sin(toward_rad)  # unit wind-toward (east, north)

        lb = length_to_breadth(speed)
        e = math.sqrt(max(0.0, 1.0 - 1.0 / (lb * lb)))
        head_rate = head_ros_m_per_min(ros_ref, wind_factor, speed, slope_percent)  # m/min

        cx, cy = _centroid(front)
        new_front: list[tuple[float, float]] = []
        for px, py in front:
            nx, ny = px - cx, py - cy
            norm = math.hypot(nx, ny) or 1.0
            nx, ny = nx / norm, ny / norm  # outward direction from centroid
            cos_phi = nx * wx + ny * wy    # alignment with wind-toward
            # Elliptical polar rate: head_rate downwind, (1-e)/(1+e)*head backing.
            rate = head_rate * (1.0 - e) / (1.0 - e * cos_phi)
            disp = rate * step_minutes     # meters advanced this step
            new_front.append((px + nx * disp, py + ny * disp))
        front = new_front

        # Head distance = farthest projection of the front onto the wind-toward axis.
        head_m = max(px * wx + py * wy for px, py in front)
        ring = [list(local_meters_to_lonlat(lat, lon, px, py)) for px, py in front]
        ring.append(ring[0])  # close
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
                "step": step_idx,
                "minutes": minutes,
                "hours": round(minutes / 60.0, 2),
                "head_distance_km": round(head_m / 1000.0, 3),
                "area_km2": round(_polygon_area_m2(front) / 1_000_000.0, 3),
                "wind_speed_kmh": round(speed, 1),
                "wind_from_deg": round(dir_from, 1),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "model": "huygens-elliptical-timevarying",
            "steps": len(features),
            "seeded_from_perimeter": initial_front is not None,
        },
    }
