#!/usr/bin/env python3
"""
Feature-parity check: confirm the features the ADAPTER computes at inference match
the features the model was TRAINED on (validation/features.csv). If they match, the
Phase-4 skill result (measured with training features) is also the production result,
and ml_correction can be trusted on by default.

For a sample of collected fires it re-sends the raw wind + momentum to /predict and
reads back parameters.ml_features, then compares to the CSV row. Backend must be up.

Usage: python validation/parity_check.py --features validation/features.csv --n 15
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

API = os.environ.get("WILDFIRE_API", "http://localhost:8000")
NUM = ["month", "log_t0", "momentum", "mean_wind", "peak_wind", "peak_gust",
       "dir_consistency", "temp_c", "rh", "vpd", "hdw", "slope_pct", "horizon_h"]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _csv_feats(row):
    t0 = _f(row["t0_km2"])
    return {"log_t0": math.log(max(t0, 1.0)),
            **{k: _f(row[k]) for k in NUM if k != "log_t0"},
            "fuel_model": row["fuel_model"]}


def _adapter_feats(row):
    t0d, t1d = date.fromisoformat(row["t0"]), date.fromisoformat(row["t1"])
    t0_geo, t1_geo, momentum = rv._fetch_geomac(row["fire"], int(row["year"]), t0d, t1d, row["state"] or None)
    from shapely.geometry import shape
    g1 = shape(t1_geo)
    olat, olon = g1.centroid.y, g1.centroid.x
    series, mean_t, mean_rh = rv._fetch_history(olat, olon, t0d, t1d)
    supp = max(0.0, min(1.0, 2.0 - momentum)) if momentum is not None else None
    body = {"lat": olat, "lon": olon, "duration_hours": (t1d - t0d).days * 24, "step_minutes": 60,
            "ignite_from_perimeter": False, "ignition_geojson": t0_geo, "wind_series": series,
            "temperature_c": mean_t, "relative_humidity": mean_rh, "season_month": t0d.month,
            "suppression": supp, "momentum": momentum}
    r = httpx.post(f"{API}/predict", json=body, timeout=300.0)
    r.raise_for_status()
    return r.json()["parameters"].get("ml_features", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=os.path.join(os.path.dirname(__file__), "features.csv"))
    ap.add_argument("--n", type=int, default=15)
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.features, encoding="utf-8")))
    random.Random(1).shuffle(rows)

    checked = 0
    diffs = {k: [] for k in NUM}
    fuel_ok = 0
    for row in rows:
        if checked >= args.n:
            break
        try:
            a = _adapter_feats(row)
            c = _csv_feats(row)
        except Exception as e:
            print(f"  skip {row['fire'][:16]}: {type(e).__name__}")
            continue
        checked += 1
        for k in NUM:
            av, cv = _f(a.get(k)), c[k]
            if av == av and cv == cv:                 # both non-nan
                denom = max(abs(cv), 1.0)
                diffs[k].append(abs(av - cv) / denom)
        fuel_ok += int(str(a.get("fuel_model")) == str(c["fuel_model"]))

    print(f"\nChecked {checked} fires. Mean relative |adapter − training| per feature:")
    for k in NUM:
        d = diffs[k]
        tag = "OK" if (d and sum(d) / len(d) < 0.02) else ("--" if not d else "DIFF")
        print(f"  {k:16} {('%.4f' % (sum(d)/len(d))) if d else 'n/a':>8}  {tag}")
    print(f"  fuel_model       {fuel_ok}/{checked} exact match")


if __name__ == "__main__":
    main()
