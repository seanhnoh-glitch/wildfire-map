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

from .geo import bearing_to_math_radians, local_meters_to_lonlat, wind_from_to_toward_bearing

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
