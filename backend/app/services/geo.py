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


def lonlat_to_local_meters(origin_lat: float, origin_lon: float, lon: float, lat: float):
    """
    Inverse of local_meters_to_lonlat: express a lon/lat as an east/north offset
    in meters from an origin, using the same equirectangular approximation.
    """
    dy_m = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    dx_m = math.radians(lon - origin_lon) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat))
    return dx_m, dy_m


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


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing (0=N, clockwise) from point 1 toward point 2, in degrees."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def point_at_distance_bearing(lat: float, lon: float, dist_km: float, bearing_deg: float):
    """Destination lon/lat reached by travelling `dist_km` along `bearing_deg` from a point."""
    ang = dist_km / (EARTH_RADIUS_M / 1000.0)          # angular distance in radians
    brg = math.radians(bearing_deg)
    phi1, lam1 = math.radians(lat), math.radians(lon)
    phi2 = math.asin(math.sin(phi1) * math.cos(ang) + math.cos(phi1) * math.sin(ang) * math.cos(brg))
    lam2 = lam1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(phi1),
        math.cos(ang) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(lam2), math.degrees(phi2)      # lon, lat


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two compass bearings, 0..180."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)
