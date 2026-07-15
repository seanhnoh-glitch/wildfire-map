#!/usr/bin/env python3
"""
Phase 4: does the ML magnitude correction actually improve SKILL (not just area
bias)? We apply the learned residual to each held-out forecast GEOMETRICALLY —
scaling the forecast footprint so its area becomes area × predicted_correction —
and re-score Jaccard vs the observed perimeter, under BY-FIRE cross-validation.

Reads features.csv (features + residual) + features_geom.jsonl (forecast/observed
footprints in local metres). Reports mean Jaccard / skill with vs without the
correction, split by fire behaviour (run / moderate / quiet).

Usage: python validation/phase4_skill.py --features validation/features.csv
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from shapely import wkt
from shapely.affinity import scale as shp_scale
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NUM = ["month", "log_t0", "momentum", "mean_wind", "peak_wind", "peak_gust",
       "dir_consistency", "temp_c", "rh", "vpd", "hdw", "slope_pct", "horizon_h"]
CAT = ["fuel_model"]


def _jac(a, b):
    if a.is_empty or b.is_empty:
        return 0.0
    if not a.is_valid:
        a = a.buffer(0)
    u = a.union(b).area
    return a.intersection(b).area / u if u > 0 else 0.0


def _rows(label, jac, base):
    jac, base = np.asarray(jac), np.asarray(base)
    n = len(jac)
    if not n:
        return
    skill = jac - base
    beat = int(np.sum(skill > 0.01))
    print(f"  {label:24} Jacc {jac.mean():.3f}   skill {skill.mean():+.3f}   beats persistence {beat}/{n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=os.path.join(os.path.dirname(__file__), "features.csv"))
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()
    geom_path = os.path.splitext(args.features)[0] + "_geom.jsonl"

    df = pd.read_csv(args.features)
    df = df[(df["residual"].notna()) & (df["pred_km2"] > 0) & (df["obs_km2"] > 0)].copy()
    geom = {}
    with open(geom_path, encoding="utf-8") as fh:
        for line in fh:
            g = json.loads(line)
            geom[tuple(g["key"])] = g
    df["key"] = list(zip(df["fire"].astype(str), df["state"].fillna("").astype(str), df["t0"].astype(str)))
    df = df[df["key"].isin(geom)].reset_index(drop=True)
    n = len(df)
    fires = (df["fire"].astype(str) + "|" + df["state"].fillna("") + "|" + df["year"].astype(str))
    print(f"{n} rows with geometry from {fires.nunique()} fires ({args.folds}-fold by-fire CV)\n")
    if n < 60 or fires.nunique() < args.folds:
        print("Too few rows/fires — collect more first.")
        return

    df["log_t0"] = np.log(df["t0_km2"].clip(lower=1))
    df["log_resid"] = np.log(df["residual"].clip(0.25, 4.0))
    X = df[NUM + CAT].copy()
    X[CAT[0]] = X[CAT[0]].astype("category")
    y = df["log_resid"].to_numpy()
    groups = fires.to_numpy()

    corr = np.ones(n)                                    # held-out predicted correction factor
    gkf = GroupKFold(n_splits=args.folds)
    for tr, te in gkf.split(X, y, groups):
        model = HistGradientBoostingRegressor(
            loss="absolute_error", max_depth=3, learning_rate=0.05, max_iter=400,
            min_samples_leaf=20, l2_regularization=1.0,
            categorical_features=[len(NUM)], random_state=0).fit(X.iloc[tr], y[tr])
        corr[te] = np.exp(np.clip(model.predict(X.iloc[te]), np.log(0.3), np.log(3.0)))

    base_j, phys_j, corr_j = [], [], []
    for i, row in df.iterrows():
        g = geom[row["key"]]
        fc, obs = wkt.loads(g["fc"]), wkt.loads(g["obs"])
        if not fc.is_valid:
            fc = fc.buffer(0)
        k = float(np.sqrt(corr[i]))                      # linear scale for an area factor
        fc_corr = shp_scale(fc, xfact=k, yfact=k, origin=(0, 0))
        base_j.append(row["base_jaccard"])
        phys_j.append(_jac(fc, obs))
        corr_j.append(_jac(fc_corr, obs))
    # grew_pct is only for splitting the RESULTS by behaviour (not a model input).
    grew = (100.0 * (df["obs_km2"] - df["t0_km2"]) / df["t0_km2"].clip(lower=1e-9)).to_numpy()
    base_j, phys_j, corr_j = map(np.asarray, (base_j, phys_j, corr_j))

    def split(mask, name):
        print(f"\n{name} ({int(mask.sum())} fires):")
        _rows("physics only", phys_j[mask], base_j[mask])
        _rows("+ ML correction", corr_j[mask], base_j[mask])

    print("=== SKILL with vs without the ML magnitude correction (held-out) ===")
    split(np.ones(n, bool), "ALL")
    split(grew >= 100, "RUN days (grew >=100%)")
    split((grew >= 10) & (grew < 100), "MODERATE (10-100%)")
    split(grew < 10, "QUIET (<10%)")


if __name__ == "__main__":
    main()
