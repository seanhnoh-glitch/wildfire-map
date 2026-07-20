#!/usr/bin/env python3
"""
Phase 2 of the ML residual-correction plan: turn harvested fire-days into a
training table.

For each example it fetches the real T0/T1 perimeters + historical wind, runs the
CURRENT physics model (gusts + spotting + regime + suppression) through /predict,
and records forecast-time FEATURES alongside the residual we want a model to learn:

    residual = observed_area / forecast_area      (the factor that would fix the
                                                   forecast's magnitude)

Only features knowable at forecast time are stored (no leakage): fire size, recent
growth momentum, month, region, and the wind/weather/fuel/terrain the forecast used.
Rows are appended to a CSV and re-runs skip finished examples, so it is resumable /
background-friendly. Split BY FIRE for any train/test evaluation.

Usage (backend on :8000, ideally with FETCH_CACHE_DIR set for reproducibility):
    python validation/build_features.py --manifest validation/batch_harvested.json \
        --limit 1200 --out validation/features.csv
"""
import argparse
import csv
import math
import os
import random
import sys
from datetime import date

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import retrospective_validation as rv  # noqa: E402
from app.services.ml_correction import wind_features  # noqa: E402  (canonical, shared w/ adapter)

API = os.environ.get("WILDFIRE_API", "http://localhost:8000")
_ACRE_KM2 = 0.00404686

FIELDS = [
    # identifiers (not features)
    "fire", "state", "year", "t0", "t1",
    # features (known at forecast time)
    "month", "t0_km2", "momentum", "mean_wind", "peak_wind", "peak_gust",
    "dir_consistency", "temp_c", "rh", "vpd", "hdw", "fuel_model", "slope_pct", "horizon_h",
    # model output + observed + TARGET
    "pred_km2", "obs_km2", "base_jaccard", "jaccard", "skill", "area_bias", "residual",
]


def _svp(t):  # saturation vapour pressure, kPa
    return 0.6108 * math.exp(17.27 * t / (t + 237.3))


def collect(ex):
    """Run one example through the model and return a feature/residual row, or None."""
    t0d, t1d = date.fromisoformat(ex["t0"]), date.fromisoformat(ex["t1"])
    horizon_h = (t1d - t0d).days * 24
    t0_geo, t1_geo, momentum = rv._fetch_geomac(ex["fire"], ex["year"], t0d, t1d, ex.get("state"))
    from shapely.geometry import shape
    g1 = shape(t1_geo)
    if not g1.is_valid:
        g1 = g1.buffer(0)
    olat, olon = g1.centroid.y, g1.centroid.x
    t0_local = rv._to_local(t0_geo, olat, olon)
    t1_local = rv._to_local(t1_geo, olat, olon)
    series, mean_t, mean_rh = rv._fetch_history(olat, olon, t0d, t1d)
    if not series:
        return None, None
    suppression = None
    if momentum is not None:
        suppression = max(0.0, min(1.0, (2.0 - momentum) / 1.0))
    # Send the RAW 3-col [sustained,dir,gust] series; the adapter gust-blends it (same
    # net wind as before) and computes the SAME wind features we use below.
    body = {
        "lat": olat, "lon": olon, "duration_hours": horizon_h, "step_minutes": 60,
        "ignite_from_perimeter": False, "ignition_geojson": t0_geo,
        "wind_series": series, "temperature_c": mean_t, "relative_humidity": mean_rh,
        "season_month": t0d.month, "suppression": suppression, "momentum": momentum,
    }
    r = httpx.post(f"{API}/predict", json=body, timeout=300.0)
    r.raise_for_status()
    resp = r.json()
    feats = resp["isochrones"].get("features") or []
    if not feats:
        return None, None
    last = max(feats, key=lambda f: f["properties"]["hours"])
    pred_local = rv._to_local(last["geometry"], olat, olon)
    m = rv._metrics(pred_local, t1_local)
    base = rv._metrics(t0_local, t1_local)
    params = resp.get("parameters", {})

    mean_w, peak_w, peak_g, dir_cons = wind_features(series)
    vpd = max(0.0, _svp(mean_t) * (1 - mean_rh / 100.0)) if (mean_t is not None and mean_rh is not None) else None
    hdw = vpd * (peak_w / 3.6) if vpd is not None else None
    pred_km2, obs_km2 = m["pred_km2"], m["obs_km2"]
    residual = obs_km2 / pred_km2 if pred_km2 > 0 else None
    # Forecast + observed footprints (local metres about the T1 centroid) for Phase 4,
    # so a magnitude correction can be applied geometrically and re-scored for skill.
    geom = {"key": [ex["fire"], ex.get("state", ""), ex["t0"]],
            "fc": pred_local.wkt, "obs": t1_local.wkt}
    row = {
        "fire": ex["fire"], "state": ex.get("state", ""), "year": ex["year"],
        "t0": ex["t0"], "t1": ex["t1"],
        "month": t0d.month, "t0_km2": round(rv._km2(t0_local), 2),
        "momentum": round(momentum, 3) if momentum is not None else "",
        "mean_wind": round(mean_w, 1), "peak_wind": round(peak_w, 1), "peak_gust": round(peak_g, 1),
        "dir_consistency": round(dir_cons, 3),
        "temp_c": round(mean_t, 1) if mean_t is not None else "",
        "rh": round(mean_rh, 1) if mean_rh is not None else "",
        "vpd": round(vpd, 2) if vpd is not None else "",
        "hdw": round(hdw, 1) if hdw is not None else "",
        "fuel_model": params.get("fuel_model", ""), "slope_pct": params.get("slope_percent", ""),
        "horizon_h": horizon_h,
        "pred_km2": round(pred_km2, 2), "obs_km2": round(obs_km2, 2),
        "base_jaccard": round(base["jaccard"], 3), "jaccard": round(m["jaccard"], 3),
        "skill": round(m["jaccard"] - base["jaccard"], 3), "area_bias": round(m["area_bias"], 3),
        "residual": round(residual, 3) if residual is not None else "",
    }
    return row, geom


def _stratified(examples, limit):
    """Sample `limit` examples keeping ~35% run / 35% moderate / 30% quiet."""
    run = [e for e in examples if e["grew_pct"] >= 100]
    mod = [e for e in examples if 10 <= e["grew_pct"] < 100]
    quiet = [e for e in examples if e["grew_pct"] < 10]
    rng = random.Random(42)
    for g in (run, mod, quiet):
        rng.shuffle(g)
    picks = run[:int(limit * 0.35)] + mod[:int(limit * 0.35)] + quiet[:int(limit * 0.30)]
    rng.shuffle(picks)
    return picks


def main():
    import json
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", default=os.path.join(os.path.dirname(__file__), "batch_harvested.json"))
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "features.csv"))
    p.add_argument("--limit", type=int, default=1200)
    args = p.parse_args()

    with open(args.manifest, encoding="utf-8") as fh:
        examples = json.load(fh)["runs"]
    picks = _stratified(examples, args.limit)

    done = set()
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            done = {(r["fire"], r["state"], r["t0"]) for r in csv.DictReader(fh)}
    write_header = not os.path.exists(args.out)
    todo = [e for e in picks if (e["fire"], e.get("state", ""), e["t0"]) not in done]
    print(f"{len(picks)} sampled, {len(done)} already done, {len(todo)} to run.")

    geom_path = os.path.splitext(args.out)[0] + "_geom.jsonl"
    ok = err = 0
    with open(args.out, "a", newline="", encoding="utf-8") as fh, \
            open(geom_path, "a", encoding="utf-8") as gh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        for i, ex in enumerate(todo, 1):
            try:
                row, geom = collect(ex)
                if row:
                    w.writerow(row); fh.flush(); ok += 1
                    gh.write(json.dumps(geom) + "\n"); gh.flush()
                    print(f"  [{i}/{len(todo)}] ok  {ex['fire'][:20]:20} {ex['t0']} "
                          f"resid={row['residual']} skill={row['skill']}")
                else:
                    err += 1
            except Exception as e:
                err += 1
                print(f"  [{i}/{len(todo)}] ERR {ex['fire'][:20]:20} {ex['t0']} {type(e).__name__}: {str(e)[:50]}")
    print(f"\nDone: {ok} rows written, {err} skipped/errored → {args.out} (+ {geom_path})")


if __name__ == "__main__":
    main()
