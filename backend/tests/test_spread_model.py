"""
Offline unit tests for the pure (no-network, no-pyforefire) logic:
geospatial helpers, perimeter parsing, fuel mapping, and the ForeFire input
math (moisture, wind adjustment, domain sizing, directional extents).

Run:  cd backend && python -m pytest        (after: pip install pytest)
"""
import math

from app.services import forefire_adapter as fa
from app.services import fuel as fuel_svc
from app.services import spread_model as sm
from app.services.fuel_table import BARRIER_FUEL_INDEX, FARSITE_FUEL_TABLE
from app.services.geo import (
    angle_diff_deg,
    haversine_km,
    initial_bearing_deg,
    local_meters_to_lonlat,
    lonlat_to_local_meters,
    point_at_distance_bearing,
    wind_from_to_toward_bearing,
)
from app.services import evacuation as evac


# --- geo -------------------------------------------------------------------

def test_wind_from_to_toward():
    assert wind_from_to_toward_bearing(270) == 90   # from west -> toward east
    assert wind_from_to_toward_bearing(0) == 180     # from north -> toward south


def test_haversine_known_distance():
    d = haversine_km(34.0, -118.0, 35.0, -118.0)     # ~1 deg lat ~ 111 km
    assert 110 < d < 112


def test_local_meters_roundtrip():
    dx, dy = lonlat_to_local_meters(39.0, -120.0, -119.99, 39.01)
    lon, lat = local_meters_to_lonlat(39.0, -120.0, dx, dy)
    assert abs(lon - (-119.99)) < 1e-6 and abs(lat - 39.01) < 1e-6


# --- perimeter geometry ----------------------------------------------------

def _square(clat, clon, half):
    ring = [
        [clon - half, clat - half], [clon + half, clat - half],
        [clon + half, clat + half], [clon - half, clat + half],
        [clon - half, clat - half],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def test_perimeter_to_polygon_centroid_and_area():
    olat, olon, poly = sm.perimeter_to_polygon(_square(34.0, -118.0, 0.02))
    assert abs(olat - 34.0) < 1e-6 and abs(olon + 118.0) < 1e-6
    assert 10 < poly.area / 1e6 < 25          # ~0.04deg square ≈ 16 km²


def test_perimeter_multipolygon_picks_largest():
    small = _square(34.0, -118.0, 0.005)["coordinates"]
    big = _square(34.0, -118.0, 0.03)["coordinates"]
    geom = {"type": "MultiPolygon", "coordinates": [small, big]}
    _, _, poly = sm.perimeter_to_polygon(geom)
    assert poly.area / 1e6 > 20               # chose the big square, not the small


def test_perimeter_to_polygon_rejects_degenerate():
    assert sm.perimeter_to_polygon({"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}) is None
    assert sm.perimeter_to_polygon(None) is None


# --- fuel mapping ----------------------------------------------------------

def test_fbfm40_int_to_code():
    assert fuel_svc._fbfm40_int_to_code(102) == "GR2"
    assert fuel_svc._fbfm40_int_to_code(165) == "TU5"
    assert fuel_svc._fbfm40_int_to_code(204) == "SB4"
    assert fuel_svc._fbfm40_int_to_code(91) is None     # non-burnable
    assert fuel_svc._fbfm40_int_to_code(120) is None    # gap


def test_fuel_value_to_index_barrier():
    assert fuel_svc._fuel_value_to_index("182") == 182          # burnable passes through
    assert fuel_svc._fuel_value_to_index("98") == BARRIER_FUEL_INDEX   # water -> barrier
    assert fuel_svc._fuel_value_to_index("91") == BARRIER_FUEL_INDEX   # urban -> barrier
    assert fuel_svc._fuel_value_to_index("NoData") == BARRIER_FUEL_INDEX


def test_get_params_preserves_code_and_defaults():
    assert fuel_svc.get_params("TL2") == {"code": "TL2", "name": "Low load broadleaf litter"}
    assert fuel_svc.get_params(None)["code"] == "GR2"           # unknown -> default
    assert fuel_svc.get_params("NB1")["code"] == "GR2"          # non-burnable -> default


def test_fuel_table_integrity():
    rows = FARSITE_FUEL_TABLE.strip().splitlines()
    assert all(len(r.split(";")) == 16 for r in rows)          # header + all rows, 16 cols
    idxs = {r.split(";")[0] for r in rows[1:]}
    for code in ["101", "109", "141", "165", "189", "204", str(BARRIER_FUEL_INDEX)]:
        assert code in idxs                                    # all FBFM40 + barrier present


# --- ForeFire input math ---------------------------------------------------

def test_met_wind_to_uv_direction():
    u, v = fa._met_wind_to_uv(10.0, 270.0)      # wind FROM west blows toward east
    assert u > 9.9 and abs(v) < 1e-6


def test_fuel_moisture_from_humidity():
    dry = fa._fuel_moisture_from_weather(30.0, 10.0)     # hot & dry
    humid = fa._fuel_moisture_from_weather(15.0, 90.0)   # cool & humid
    assert 0 < dry["ones"] < dry["tens"] < dry["hundreds"]
    assert dry["ones"] < humid["ones"]                   # drier air -> drier fuel
    assert fa._fuel_moisture_from_weather(None, None) == fa._DEFAULT_MOISTURE


def test_wind_adjustment_factor_range():
    waf_grass = fa._wind_adjustment_factor("GR2")
    waf_shrub = fa._wind_adjustment_factor("SH5")
    assert 0.25 < waf_grass < 0.5
    assert waf_shrub > waf_grass                         # deeper fuel bed -> less reduction
    assert fa._wind_adjustment_factor("ZZ9") == waf_grass  # unknown -> default depth (GR2-like)


def test_domain_extents():
    from shapely.geometry import Point
    assert fa._domain_extents(None) == (40_000.0, 2_000.0)      # point ignition
    half, fire_half = fa._domain_extents(Point(0, 0).buffer(10_000))
    assert abs(fire_half - 10_000) < 200 and half == 40_000.0   # fits in the floor
    big_half, _ = fa._domain_extents(Point(0, 0).buffer(80_000))
    assert big_half == 100_000.0                                # grows: 80k + 20k margin


def test_directional_extents_downwind_exceeds_backing():
    # A ring shifted east; with wind toward the east (90°) downwind > backing.
    origin_lat, origin_lon = 40.0, -120.0
    ring = [list(local_meters_to_lonlat(origin_lat, origin_lon, x, y))
            for x, y in [(6000, 1000), (6000, -1000), (-2000, -1000), (-2000, 1000), (6000, 1000)]]
    dw, bk, cross = fa._directional_extents(ring, origin_lat, origin_lon, 90.0)
    assert dw > bk                                              # elongated downwind (east)
    assert dw > 5.0 and cross > 0


# --- evacuation routing (offline pieces) -----------------------------------

def test_initial_bearing_cardinals():
    assert abs(initial_bearing_deg(37, -119, 38, -119)) < 0.01          # due north
    assert abs(initial_bearing_deg(37, -119, 37, -118) - 90) < 1.0      # due east


def test_point_at_distance_bearing_roundtrip():
    lon, lat = point_at_distance_bearing(37.0, -119.0, 50.0, 90.0)      # 50 km east
    assert lat == __import__("pytest").approx(37.0, abs=0.05)
    assert 49 < haversine_km(37.0, -119.0, lat, lon) < 51


def test_angle_diff_wraps():
    assert angle_diff_deg(350, 10) == 20
    assert angle_diff_deg(10, 350) == 20
    assert angle_diff_deg(0, 180) == 180


def test_danger_union_from_featurecollection():
    poly = _square(38.0, -119.0, 0.1)
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": poly}]}
    geom, centroid = evac._danger_union(fc, None)
    assert geom is not None
    assert centroid == __import__("pytest").approx((-119.0, 38.0), abs=1e-6)


def test_away_from_fire_destinations_ranked_away():
    # Fire to the west; user east of it. Safe points should head further east
    # (away), and a point back toward the fire should rank worse.
    fire = (-119.0, 38.0)
    user_lat, user_lon = 38.0, -118.5
    danger, _ = evac._danger_union(_square(38.0, -119.0, 0.05), None)
    away = initial_bearing_deg(fire[1], fire[0], user_lat, user_lon)     # ~east
    assert 60 < away < 120
    dests = evac._geometric_safe_points(user_lat, user_lon, fire, away)
    ranked = evac._filter_and_rank_destinations(dests, user_lat, user_lon, fire, danger, away)
    assert ranked                                                        # something survives
    # Every kept destination is clear of the fire by the safe buffer.
    assert all(d["km_from_fire"] >= evac._SAFE_BUFFER_KM for d in ranked)
    # The best-ranked one heads away from the fire (east-ish), not back toward it.
    assert angle_diff_deg(ranked[0]["bearing"], away) < 90


def test_geom_length_km_handles_mixed_geometry():
    from shapely.geometry import GeometryCollection, LineString, Point, Polygon
    ln = LineString([(-119.0, 37.0), (-118.0, 37.0)])          # ~1° lon at 37°N ≈ 89 km
    L = evac._geom_length_km(ln)
    assert 85 < L < 92
    # A GeometryCollection (as line∩polygon can produce) — only the line counts.
    gc = GeometryCollection([ln, Point(-119.0, 37.0), Polygon([(0, 0), (1, 0), (1, 1)])])
    assert abs(evac._geom_length_km(gc) - L) < 1e-6
    assert evac._geom_length_km(None) == 0.0


def _danger_hit_km(user_lon, user_lat, route_line, danger):
    """Replicate _mapbox_routes' danger measure: in-fire route length beyond the
    depth-aware origin-ignore disk."""
    from shapely.geometry import Point
    op = Point(user_lon, user_lat)
    depth_km = op.distance(danger.boundary) * 111.0 if danger.contains(op) else 0.0
    ignore = op.buffer((evac._ORIGIN_IGNORE_KM + depth_km) / 111.0)
    return evac._geom_length_km(route_line.intersection(danger).difference(ignore))


def test_route_into_fire_flagged_but_away_route_clear():
    from shapely.geometry import LineString
    danger, _ = evac._danger_union(_square(38.0, -119.0, 0.05), None)   # ~11 km box
    ulon, ulat = -118.85, 38.0                                          # user east of, outside, the fire
    # Heading further east, away from the fire — never enters it.
    away = LineString([(ulon, ulat), (-118.4, 38.0)])
    assert _danger_hit_km(ulon, ulat, away, danger) <= evac._DANGER_HIT_KM
    # Driving west straight through the fire — flagged.
    into = LineString([(ulon, ulat), (-119.3, 38.0)])
    assert _danger_hit_km(ulon, ulat, into, danger) > evac._DANGER_HIT_KM


def test_origin_ignore_grows_with_depth_inside_fire():
    from shapely.geometry import Point
    danger, _ = evac._danger_union(_square(38.0, -119.0, 0.05), None)
    assert not danger.contains(Point(-118.8, 38.0))                     # outside -> no allowance
    center = Point(-119.0, 38.0)
    assert danger.contains(center)                                     # inside
    assert center.distance(danger.boundary) * 111.0 > 3.0             # depth allowance is real
