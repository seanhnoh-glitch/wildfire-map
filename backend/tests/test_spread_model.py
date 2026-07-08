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
    haversine_km,
    local_meters_to_lonlat,
    lonlat_to_local_meters,
    wind_from_to_toward_bearing,
)


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
