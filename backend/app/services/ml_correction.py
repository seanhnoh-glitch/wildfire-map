"""
ML residual correction (Phase 5) — a thin inference wrapper around the gradient-
boosted model trained by validation/train_correction.py.

Given forecast-time features it predicts the multiplicative area correction
(observed/forecast that the physics model tends to miss) and rescales the forecast
footprint accordingly. Validated by validation/phase4_skill.py: it lifts Jaccard on
the fires the surface model over-predicts (moderate/quiet days) and leaves run days
essentially unchanged.

Fully optional: if the model file or its libraries (scikit-learn/joblib) are absent,
every function no-ops so the engine still runs. Enabled by config.ml_correction.
"""
import functools
import math
import os

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "..", "ml", "correction_model.joblib")


def wind_features(raw_series):
    """Canonical wind features from a RAW wind series [[sustained_kmh, from_deg,
    gust_kmh?], ...]. Used by BOTH the training pipeline (validation/build_features.py)
    and live inference (forefire_adapter._ml_features) so the model sees identical
    features. Returns (mean_wind, peak_wind, peak_gust, dir_consistency); gust falls
    back to sustained when absent, dir_consistency is 1 for a steady heading → 0 for
    a highly variable one."""
    if not raw_series:
        return (float("nan"),) * 4
    spd = [float(s[0]) for s in raw_series]
    gst = [float(s[2]) if len(s) > 2 else float(s[0]) for s in raw_series]
    dirs = [float(s[1]) for s in raw_series]
    cx = sum(math.cos(math.radians(d)) for d in dirs) / len(dirs)
    cy = sum(math.sin(math.radians(d)) for d in dirs) / len(dirs)
    return (sum(spd) / len(spd), max(spd), max(gst), math.hypot(cx, cy))


@functools.lru_cache(maxsize=1)
def _bundle():
    try:
        import joblib
        return joblib.load(_MODEL_PATH)
    except Exception:
        return None


def available() -> bool:
    """True when the trained model can be loaded."""
    return _bundle() is not None


def correction_factor(feat: dict):
    """Predicted area-correction factor for one forecast, from a dict of the training
    feature names (missing values -> NaN, which the model handles). None if the model
    or its libraries are unavailable. Clamped to the model's trained range."""
    b = _bundle()
    if b is None:
        return None
    try:
        import numpy as np
        import pandas as pd
        num, cat, (lo, hi) = b["num"], b["cat"], b["clip"]
        data = {k: [feat.get(k, np.nan)] for k in num}
        data[cat[0]] = pd.Categorical([feat.get(cat[0])])
        X = pd.DataFrame(data)[num + cat]
        val = float(b["model"].predict(X)[0])
        return float(min(hi, max(lo, math.exp(val))))
    except Exception:
        return None


def scale_isochrones(isochrones: dict, factor: float, origin_lat: float,
                     origin_lon: float, t0_local=None) -> dict:
    """Rescale the forecast footprint toward the learned observed/forecast `factor`,
    RAMPED IN OVER THE FORECAST HORIZON: the model learned the area bias at the full
    (e.g. 24 h) horizon, so applying it uniformly would also inflate the near-term
    isochrones and make the forecast "start" bigger than the fire's current perimeter.
    Instead each isochrone at fraction t of the horizon is scaled by
    factor**t (so t≈0 → ×1 ≈ the current perimeter, t=1 → the full factor), about the
    FORECAST centroid, then UNIONed with the ignition footprint `t0_local` (shapely,
    local metres) so it is never smaller than the current perimeter. Edits in place."""
    if not factor or abs(factor - 1.0) < 1e-3:
        return isochrones
    from shapely.affinity import scale as shp_scale
    from shapely.geometry import Polygon, mapping
    from shapely.ops import transform, unary_union
    from .geo import local_meters_to_lonlat, lonlat_to_local_meters

    feats = [f for f in isochrones.get("features", [])
             if (f.get("geometry") or {}).get("type") == "Polygon"]
    if not feats:
        return isochrones
    max_h = max((f.get("properties") or {}).get("hours", 0) for f in feats) or 1.0

    def to_local(f):
        ring = (f["geometry"].get("coordinates") or [[]])[0]
        p = Polygon([lonlat_to_local_meters(origin_lat, origin_lon, lo, la) for lo, la in ring])
        return p if p.is_valid else p.buffer(0)

    def _to_lonlat(x, y, z=None):
        return local_meters_to_lonlat(origin_lat, origin_lon, x, y)   # -> (lon, lat)

    final = to_local(max(feats, key=lambda f: (f.get("properties") or {}).get("hours", 0)))
    if final.is_empty:
        return isochrones
    cx, cy = final.centroid.x, final.centroid.y
    for f in feats:
        p = to_local(f)
        if p.is_empty:
            continue
        # Time-ramped correction: full `factor` only at the horizon, ~1 near t0.
        frac = min(1.0, max(0.0, (f.get("properties") or {}).get("hours", 0) / max_h))
        k = math.sqrt(max(1e-6, factor ** frac))
        p = shp_scale(p, xfact=k, yfact=k, origin=(cx, cy))
        if t0_local is not None and not t0_local.is_empty:
            p = unary_union([p, t0_local])            # anchor: never below the perimeter
        f["geometry"] = mapping(transform(_to_lonlat, p))
        props = f.get("properties") or {}
        if "area_km2" in props:
            props["area_km2"] = round(p.area / 1_000_000.0, 3)
    return isochrones
