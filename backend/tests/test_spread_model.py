"""
Tests for the built-in spread model. Pure/offline — no network.

Run:  cd backend && ./.venv/Scripts/python -m pytest   (after: pip install pytest)
"""
import math

from app.services import spread_model as sm
from app.services.geo import haversine_km, wind_from_to_toward_bearing


def test_wind_from_to_toward():
    assert wind_from_to_toward_bearing(270) == 90   # from west -> toward east
    assert wind_from_to_toward_bearing(0) == 180     # from north -> toward south


def test_calm_wind_is_near_circular():
    lb = sm.length_to_breadth(0.0)
    assert abs(lb - 1.0) < 0.05


def test_higher_wind_more_elongated():
    assert sm.length_to_breadth(40) > sm.length_to_breadth(10) > sm.length_to_breadth(0)


def test_head_ros_increases_with_wind_and_slope():
    base = sm.head_ros_m_per_min(9.0, 1.1, 0, 0)
    windy = sm.head_ros_m_per_min(9.0, 1.1, 30, 0)
    steep = sm.head_ros_m_per_min(9.0, 1.1, 30, 40)
    assert steep > windy > base > 0


def test_isochrones_nested_and_pushed_downwind():
    fc = sm.simulate(
        lat=34.05, lon=-118.24, duration_hours=6, step_minutes=60,
        wind_speed_kmh=25, wind_direction_deg=270,  # from west -> pushes east
        ros_ref=9.0, wind_factor=1.1, slope_percent=0,
    )
    feats = fc["features"]
    assert len(feats) == 6
    # Head distance grows monotonically.
    dists = [f["properties"]["head_distance_km"] for f in feats]
    assert dists == sorted(dists)
    # The far tip should be east of the ignition longitude (fire pushed east).
    last_ring = feats[-1]["geometry"]["coordinates"][0]
    east_tip = max(c[0] for c in last_ring)
    assert east_tip > -118.24
    # Ignition sits near the rear (western) edge, not the center.
    west_tip = min(c[0] for c in last_ring)
    assert abs(west_tip - (-118.24)) < abs(east_tip - (-118.24))


def test_area_positive():
    fc = sm.simulate(
        lat=40.0, lon=-120.0, duration_hours=3, step_minutes=60,
        wind_speed_kmh=15, wind_direction_deg=180,
        ros_ref=9.0, wind_factor=1.1, slope_percent=5,
    )
    assert all(f["properties"]["area_km2"] > 0 for f in fc["features"])


def test_haversine_known_distance():
    # ~roughly one degree of latitude is ~111 km
    d = haversine_km(34.0, -118.0, 35.0, -118.0)
    assert 110 < d < 112


# --- Time-varying (Huygens) propagation --------------------------------------

def _tip(feature, axis="lon"):
    ring = feature["geometry"]["coordinates"][0]
    i = 0 if axis == "lon" else 1
    return max(c[i] for c in ring), min(c[i] for c in ring)


def test_timevarying_constant_wind_nested_and_downwind():
    # Constant wind from the west for 6 h -> fire grows eastward, nested.
    series = [(25.0, 270.0)] * 6
    fc = sm.simulate_timevarying(
        lat=34.05, lon=-118.24, wind_series=series,
        ros_ref=9.0, wind_factor=1.1, slope_percent=0, step_minutes=60,
    )
    feats = fc["features"]
    assert len(feats) == 6
    areas = [f["properties"]["area_km2"] for f in feats]
    assert areas == sorted(areas) and areas[0] > 0            # monotonic growth
    east_tip, _ = _tip(feats[-1])
    assert east_tip > -118.24                                 # pushed east


def test_timevarying_windshift_bends_fire_south():
    # 3 h wind from the west (pushes east), then 3 h from the north (pushes south).
    shifting = [(25.0, 270.0)] * 3 + [(25.0, 0.0)] * 3
    steady = [(25.0, 270.0)] * 6
    fc_shift = sm.simulate_timevarying(
        lat=34.05, lon=-118.24, wind_series=shifting,
        ros_ref=9.0, wind_factor=1.1, slope_percent=0, step_minutes=60,
    )
    fc_steady = sm.simulate_timevarying(
        lat=34.05, lon=-118.24, wind_series=steady,
        ros_ref=9.0, wind_factor=1.1, slope_percent=0, step_minutes=60,
    )
    # After the shift, the front must reach farther SOUTH than the steady-wind run.
    _, south_shift = _tip(fc_shift["features"][-1], axis="lat")
    _, south_steady = _tip(fc_steady["features"][-1], axis="lat")
    assert south_shift < south_steady - 0.005                 # clearly bent south


def test_timevarying_calm_is_roughly_round():
    series = [(0.0, 270.0)] * 3
    fc = sm.simulate_timevarying(
        lat=40.0, lon=-120.0, wind_series=series,
        ros_ref=9.0, wind_factor=1.1, slope_percent=0, step_minutes=60,
    )
    east, west = _tip(fc["features"][-1])
    # near-symmetric about the origin longitude when there's no wind
    assert abs((east + 120.0) + (west + 120.0)) < 0.01
