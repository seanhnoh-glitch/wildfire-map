"""
Perimeter geometry helpers for the prediction pipeline.

Turns a raw NIFC/WFIGS GeoJSON fire perimeter into a shapely Polygon in local
metres about the perimeter centroid, which the ForeFire adapter seeds its initial
FireFront from (see forefire_adapter._seed_firefront).

Note: this module used to also contain a self-contained elliptical spread model
that served as a fallback engine. That model has been removed — ForeFire is the
only prediction engine now — leaving just the perimeter-to-polygon conversion.
"""
from typing import Optional

from shapely.geometry import Polygon

from .geo import lonlat_to_local_meters


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
