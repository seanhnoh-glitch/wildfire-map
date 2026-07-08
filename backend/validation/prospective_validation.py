#!/usr/bin/env python3
"""
Prospective validation for the ForeFire spread forecast.

Idea: you can't know if a forecast is right until the fire actually moves. So we
snapshot a forecast for a currently-active fire NOW, wait ~a day, then score the
predicted footprint against the fire's real (re-mapped) perimeter. This uses the
live pipeline unchanged — no historical data needed.

Workflow (backend must be running, e.g. the Docker image on :8000):

    # 1. Find good candidates (large, actively spreading, has a mapped perimeter)
    python prospective_validation.py candidates

    # 2. Snapshot a forecast now (saves forecast + the current perimeter as T0)
    python prospective_validation.py snapshot --lat 40.12 --lon -121.34 --hours 24

    # 3. A day or two later, score it against the fire's new perimeter
    python prospective_validation.py score --file snapshots/<file>.json

Scoring reports, for the forecast footprint vs the observed later perimeter:
  - Jaccard (intersection/union) and Sørensen–Dice — overlap, 1.0 = perfect
  - area bias (predicted/observed) — over- or under-prediction
  - a PERSISTENCE baseline (the T0 perimeter vs the observed one): if the forecast
    doesn't beat "assume the fire didn't move," it added no skill.
It also writes a GeoJSON overlay (T0 / forecast / observed) you can drop into
https://geojson.io or QGIS.

IMPORTANT — interpreting scores: this is a free-spread surface model. Real fires
are shaped by SUPPRESSION (crews stopping them), spotting, and crown fire, none of
which we model — so expect systematic OVER-prediction and modest overlap even when
the integration is perfect. Read the number as "skill vs unsuppressed spread," and
prefer fires with low containment. See README.md.

Requires: httpx, shapely (both in the backend's requirements).
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone

import httpx
from shapely.geometry import shape
from shapely.ops import transform

# Windows consoles default to cp1252, which can't encode some glyphs we print
# (arrows, em-dashes, ²). Force UTF-8 so output never crashes.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

API = os.environ.get("WILDFIRE_API", "http://localhost:8000")
SNAP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
_R = 6_371_000.0


# --- geometry helpers ------------------------------------------------------

def _to_local(geojson_geom, olat, olon):
    """GeoJSON geometry (lon/lat) → shapely geometry in local metres about an
    origin (equirectangular, matching the app's geo projection)."""
    coslat = math.cos(math.radians(olat))

    def _t(xs, ys, zs=None):
        dx = [math.radians(x - olon) * _R * coslat for x in xs]
        dy = [math.radians(y - olat) * _R for y in ys]
        return (dx, dy)

    g = shape(geojson_geom)
    if not g.is_valid:
        g = g.buffer(0)
    return transform(_t, g)


def _km2(geom_local):
    return geom_local.area / 1_000_000.0


def _metrics(pred_local, obs_local):
    inter = pred_local.intersection(obs_local).area
    union = pred_local.union(obs_local).area
    pa, oa = pred_local.area, obs_local.area
    return {
        "jaccard": inter / union if union else 0.0,
        "dice": 2 * inter / (pa + oa) if (pa + oa) else 0.0,
        "area_bias": pa / oa if oa else float("inf"),
        "pred_km2": pa / 1e6,
        "obs_km2": oa / 1e6,
        "overlap_km2": inter / 1e6,
    }


# --- API + perimeter selection ---------------------------------------------

def _get(path, **params):
    r = httpx.get(f"{API}{path}", params=params, timeout=60.0)
    r.raise_for_status()
    return r.json()


def _post(path, body):
    r = httpx.post(f"{API}{path}", json=body, timeout=300.0)
    r.raise_for_status()
    return r.json()


def _pick_perimeter(perimeters, lon, lat, name=None):
    """Choose the perimeter for this fire: name match first, then the one
    containing the point, then the nearest. Returns (feature, name) or (None, None)."""
    feats = (perimeters or {}).get("features") or []
    if not feats:
        return None, None
    from shapely.geometry import Point
    pt = Point(lon, lat)
    if name:
        for f in feats:
            if (f.get("properties") or {}).get("poly_IncidentName", "").strip().upper() == name.strip().upper():
                return f, name
    best, bestd = None, float("inf")
    for f in feats:
        try:
            g = shape(f["geometry"])
            if not g.is_valid:
                g = g.buffer(0)
        except Exception:
            continue
        if g.contains(pt):
            return f, (f.get("properties") or {}).get("poly_IncidentName")
        d = pt.distance(g)
        if d < bestd:
            bestd, best = d, f
    return best, (best.get("properties") or {}).get("poly_IncidentName") if best else None


# --- commands --------------------------------------------------------------

def cmd_candidates(args):
    """List fires worth validating: sizeable, actively spreading (low containment),
    and with a mapped perimeter."""
    fires = _get("/fires/all", min_acres=args.min_acres, limit=2000)
    rows = []
    for f in fires:
        pc = f.get("percent_contained")
        if pc is not None and pc >= args.max_contained:
            continue
        rows.append(f)
    rows.sort(key=lambda f: -(f.get("size_acres") or 0))
    print(f"{'name':32} {'acres':>9} {'cont%':>6} {'lat':>8} {'lon':>10}  (low containment = still spreading)")
    for f in rows[:args.limit]:
        print(f"{(f['name'] or '')[:32]:32} {int(f.get('size_acres') or 0):>9} "
              f"{('' if f.get('percent_contained') is None else int(f['percent_contained'])):>6} "
              f"{f['lat']:>8.3f} {f['lon']:>10.3f}")
    print("\nPick one that's LARGE and LOW containment, then:")
    print("  python prospective_validation.py snapshot --lat <lat> --lon <lon> --hours 24")


def cmd_snapshot(args):
    lat, lon = args.lat, args.lon
    near = _get("/fires/nearby", lat=lat, lon=lon, radius_km=25)
    perim, name = _pick_perimeter(near.get("perimeters"), lon, lat, args.name)
    if perim is None:
        print("!! No mapped perimeter near this point — a footprint forecast can't be scored.\n"
              "   Pick a fire that has a perimeter (see `candidates`).")
        sys.exit(1)
    print(f"Fire: {name or '(unnamed)'}  - forecasting {args.hours} h ...")
    pred = _post("/predict", {"lat": lat, "lon": lon, "duration_hours": args.hours, "step_minutes": 60})

    os.makedirs(SNAP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc)
    slug = (name or f"{lat:.3f}_{lon:.3f}").strip().replace(" ", "_").replace("/", "-")[:40]
    path = os.path.join(SNAP_DIR, f"{ts.strftime('%Y%m%dT%H%M%SZ')}_{slug}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "snapshot_utc": ts.isoformat(),
            "lat": lat, "lon": lon, "fire_name": name,
            "horizon_hours": args.hours,
            "t0_perimeter": perim["geometry"],
            "forecast": pred["isochrones"],
            "parameters": pred.get("parameters"),
            "notes": pred.get("notes"),
        }, fh)
    n_iso = len(pred["isochrones"].get("features", []))
    print(f"Saved snapshot: {path}")
    print(f"  T0 perimeter recorded, {n_iso} forecast isochrones.")
    print(f"  Score it in ~{args.hours} h (once the perimeter is re-mapped):")
    print(f"  python prospective_validation.py score --file {path}")


def cmd_score(args):
    with open(args.file, encoding="utf-8") as fh:
        snap = json.load(fh)
    lat, lon, name = snap["lat"], snap["lon"], snap.get("fire_name")
    t0 = datetime.fromisoformat(snap["snapshot_utc"])
    elapsed_h = (datetime.now(timezone.utc) - t0).total_seconds() / 3600.0
    print(f"Fire: {name or '(unnamed)'}  - snapshot {t0.isoformat()}  (+{elapsed_h:.1f} h ago)")
    if elapsed_h < 1:
        print("!! Only just snapshotted — wait for the fire to move and be re-mapped.")

    # Observed perimeter now (T1)
    near = _get("/fires/nearby", lat=lat, lon=lon, radius_km=25)
    obs, _ = _pick_perimeter(near.get("perimeters"), lon, lat, name)
    if obs is None:
        print("!! No observed perimeter available now — try again later.")
        sys.exit(1)

    # Pick the forecast isochrone whose horizon is closest to elapsed time.
    feats = snap["forecast"]["features"]
    tgt = min(elapsed_h, max(f["properties"]["hours"] for f in feats))
    pred_feat = min(feats, key=lambda f: abs(f["properties"]["hours"] - tgt))
    used_h = pred_feat["properties"]["hours"]

    # Everything into one local metric frame centred on the fire.
    pred = _to_local(pred_feat["geometry"], lat, lon)
    observed = _to_local(obs["geometry"], lat, lon)
    t0poly = _to_local(snap["t0_perimeter"], lat, lon)

    m = _metrics(pred, observed)
    base = _metrics(t0poly, observed)                    # persistence baseline
    moved = 1.0 - (t0poly.intersection(observed).area / max(t0poly.union(observed).area, 1e-9))

    print(f"\nForecast horizon used: +{used_h:.0f} h (closest to +{elapsed_h:.1f} h elapsed)")
    print(f"Areas (km²):  T0 {_km2(t0poly):8.2f}   forecast {m['pred_km2']:8.2f}   observed {m['obs_km2']:8.2f}")
    if moved < 0.02:
        print("\n!! Observed perimeter is essentially unchanged from T0 — it likely hasn't\n"
              "   been re-mapped yet, or the fire didn't spread. Score again later.")
    print(f"\n{'metric':<14}{'FORECAST vs observed':>22}{'  persistence (T0) baseline':>28}")
    print(f"{'Jaccard':<14}{m['jaccard']:>22.3f}{base['jaccard']:>28.3f}")
    print(f"{'Dice':<14}{m['dice']:>22.3f}{base['dice']:>28.3f}")
    print(f"{'area bias':<14}{m['area_bias']:>22.2f}{base['area_bias']:>28.2f}")
    skill = m["jaccard"] - base["jaccard"]
    verdict = "adds skill over persistence" if skill > 0.01 else \
              ("no better than 'no change'" if skill > -0.01 else "worse than persistence")
    print(f"\nForecast Jaccard - baseline = {skill:+.3f}  -> {verdict}")

    # Overlay for geojson.io / QGIS
    out = args.overlay or os.path.splitext(args.file)[0] + "_overlay.geojson"
    styled = {
        "type": "FeatureCollection", "features": [
            _styled(snap["t0_perimeter"], "T0 perimeter (start)", "#3388ff"),
            _styled(pred_feat["geometry"], f"forecast +{used_h:.0f}h", "#ff8800"),
            _styled(obs["geometry"], "observed perimeter (now)", "#d00000"),
        ]}
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(styled, fh)
    print(f"\nOverlay written: {out}  (open in https://geojson.io)")


def _styled(geom, title, color):
    return {"type": "Feature", "geometry": geom,
            "properties": {"title": title, "stroke": color, "stroke-width": 2,
                           "fill": color, "fill-opacity": 0.15}}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("candidates", help="list active, spreading fires with perimeters")
    c.add_argument("--min-acres", type=float, default=1000)
    c.add_argument("--max-contained", type=float, default=50, help="only fires below this %% contained")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(func=cmd_candidates)

    s = sub.add_parser("snapshot", help="save a forecast + current perimeter for a fire")
    s.add_argument("--lat", type=float, required=True)
    s.add_argument("--lon", type=float, required=True)
    s.add_argument("--name", type=str, default=None, help="incident name (optional, for matching)")
    s.add_argument("--hours", type=int, default=24)
    s.set_defaults(func=cmd_snapshot)

    sc = sub.add_parser("score", help="score a saved snapshot against the fire's new perimeter")
    sc.add_argument("--file", type=str, required=True)
    sc.add_argument("--overlay", type=str, default=None)
    sc.set_defaults(func=cmd_score)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
