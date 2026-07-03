"""
Small geospatial helpers shared across services: distance, and a local
azimuthal projection so the spread model can work in meters and hand back
lon/lat GeoJSON.
"""
import math

EARTH_RADIUS_M = 6_371_000.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lon/lat points, in kilometers."""
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    )
    return (EARTH_RADIUS_M / 1000.0) * 2 * math.asin(math.sqrt(a))


def local_meters_to_lonlat(origin_lat: float, origin_lon: float, dx_m: float, dy_m: float):
    """
    Convert an east/north offset in meters (from an origin lon/lat) back to
    lon/lat using an equirectangular approximation. Accurate to well within a
    meter over the tens-of-km scale a fire forecast spans, and dependency-free.

    dx_m: meters east (+) / west (-)
    dy_m: meters north (+) / south (-)
    """
    dlat = (dy_m / EARTH_RADIUS_M) * (180.0 / math.pi)
    dlon = (dx_m / (EARTH_RADIUS_M * math.cos(math.radians(origin_lat)))) * (180.0 / math.pi)
    return origin_lon + dlon, origin_lat + dlat


def bearing_to_math_radians(bearing_deg: float) -> float:
    """
    Convert a compass bearing (0=N, 90=E, clockwise) to standard math radians
    (0=E, counter-clockwise) for use with cos/sin on an x=east, y=north plane.
    """
    return math.radians(90.0 - bearing_deg)


def wind_from_to_toward_bearing(wind_from_deg: float) -> float:
    """
    Weather reports the direction wind blows FROM. Fire is pushed the opposite
    way (the direction it blows TOWARD). Returns the toward-bearing in degrees.
    """
    return (wind_from_deg + 180.0) % 360.0
