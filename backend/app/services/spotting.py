"""
Crown-fire ember spotting — a physically-motivated enhancement on top of ForeFire's
surface (Farsite/Rothermel) footprint.

A surface model has no concept of crown fire or firebrands, so on the plume-driven
run days of large timber fires it under-predicts: the real fire throws embers that
start new fires ahead of and beside the front, which then coalesce. This module
adds that missing mechanism as a post-step enhancement of each fire front:

  * Only where the fuel is crownable (timber / tall heavy shrub) AND it is dry and
    windy enough to loft and ignite firebrands (else it returns the front unchanged).
  * From the front's downwind-facing leading edge it launches a DETERMINISTIC fan of
    ember colonies — several launch points across the front width × a few distances ×
    a lateral spread — so the footprint grows into a broad fan, not just a longer
    downwind tongue. (A longer tongue is what a plain wind-speed increase produces,
    and validation shows that only overshoots; the lateral spread is the point.)

Deterministic (no RNG) so validation stays reproducible. Gated by
config.crown_spotting (on by default) and scaled by the fire-weather regime.
"""
import math

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .geo import local_meters_to_lonlat, lonlat_to_local_meters

# Crownable FBFM40 fuels: timber understory/litter (TU, TL) and the taller, heavier
# shrub models. Grass and light shrub don't sustain crown fire / lofting embers.
_CROWNABLE_SHRUB = {"SH5", "SH7", "SH8", "SH9", "GS3", "GS4"}
# Above this 1-h dead fuel moisture, firebrands are unlikely to ignite receptive
# fuel on landing, so spotting is suppressed.
_SPOT_MOISTURE_MAX = 0.12
# Below this ambient wind (km/h) there is no meaningful medium-range spotting.
_SPOT_WIND_MIN_KMH = 18.0


def is_crownable(fuel_code: str) -> bool:
    fc = (fuel_code or "").upper()
    return fc[:2] in ("TU", "TL") or fc in _CROWNABLE_SHRUB


def spot_distance_m(wind_kmh: float) -> float:
    """Approximate maximum downwind firebrand transport (m) as a function of the
    ambient 10 m wind. ~0 below the wind threshold, ~1 km at 40 km/h, capped at
    2.5 km — an order-of-magnitude match to observed short/medium-range spotting in
    wind-driven crown fire (full Albini spotting needs a plume/torching-tree model)."""
    w = max(0.0, wind_kmh - _SPOT_WIND_MIN_KMH)
    return min(2500.0, 45.0 * w)


def _spot_colonies(base: Polygon, dmax: float, wind_toward_deg: float) -> list:
    """A deterministic fan of ember-colony disks ahead of the front's leading edge."""
    trad = math.radians(wind_toward_deg)
    tx, ty = math.sin(trad), math.cos(trad)     # downwind unit (east, north)
    px, py = -ty, tx                             # crosswind unit
    coords = list(base.exterior.coords)
    proj = [(x * tx + y * ty, x, y) for x, y in coords]   # along-wind distance
    amin = min(p[0] for p in proj)
    amax = max(p[0] for p in proj)
    thresh = amin + 0.6 * (amax - amin)          # keep the downwind-leading 40%
    lead = [(x, y) for a, x, y in proj if a >= thresh]
    if not lead:
        return []
    stride = max(1, len(lead) // 12)             # ≤ ~12 launch points across the head
    lead = lead[::stride]
    radius = max(200.0, 0.22 * dmax)             # ember-colony blob radius (sized to
    colonies = []                                # bridge to its neighbours in the fan)
    for x, y in lead:
        for frac in (0.2, 0.4, 0.6, 0.8, 1.0):   # stepped out to the max spot distance
            d = dmax * frac
            for lat_off in (-0.3, 0.0, 0.3):     # lateral scatter → a fan, not a line
                cx = x + tx * d + px * (lat_off * d)
                cy = y + ty * d + py * (lat_off * d)
                colonies.append(Point(cx, cy).buffer(radius, quad_segs=6))
    return colonies


def enhance_ring(ring, origin_lat, origin_lon, wind_kmh, wind_toward_deg,
                 fuel_code, dead_1h, prev_poly=None, intensity=1.0):
    """
    Enhance one ForeFire fire-front ring ([lon,lat] vertices) with crown-fire
    spotting. Returns (new_ring [[lon,lat],...], polygon_in_local_metres). When
    conditions don't support spotting the ring is returned unchanged. `prev_poly`
    (the previous step's enhanced polygon, local metres) is unioned in so the
    isochrones stay nested (burned area only grows). `intensity` (0..1, the fire-
    weather regime) scales the spotting reach — near 0 on calm/humid days it
    effectively disables spotting."""
    pts = [lonlat_to_local_meters(origin_lat, origin_lon, lon, lat) for lon, lat in ring]
    if len(pts) < 3:
        return ring, prev_poly
    base = Polygon(pts)
    if not base.is_valid:
        base = base.buffer(0)
    # buffer(0) on a self-intersecting ring can split into a MultiPolygon; take the
    # largest piece so _spot_colonies (which reads .exterior) has a single polygon.
    if base.geom_type == "MultiPolygon":
        base = max(base.geoms, key=lambda g: g.area) if not base.is_empty else base
    if base.is_empty or base.geom_type != "Polygon":
        return ring, prev_poly
    poly = base
    dry_enough = dead_1h is None or dead_1h <= _SPOT_MOISTURE_MAX
    if is_crownable(fuel_code) and dry_enough:
        dmax = spot_distance_m(wind_kmh) * max(0.0, min(2.0, intensity))
        if dmax >= 100.0:
            colonies = _spot_colonies(base, dmax, wind_toward_deg)
            if colonies:
                merged = unary_union([base] + colonies)
                # Morphological closing bridges the discrete spot colonies and the
                # front into one contiguous burned footprint (spot fires merge with
                # the main fire over the forecast window).
                bridge = 0.25 * dmax
                merged = merged.buffer(bridge).buffer(-bridge)
                if not merged.is_empty and merged.is_valid:
                    poly = merged
    if prev_poly is not None and not prev_poly.is_empty:
        poly = unary_union([poly, prev_poly])
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    ring_out = [list(local_meters_to_lonlat(origin_lat, origin_lon, x, y))
                for x, y in poly.exterior.coords]
    return ring_out, poly
