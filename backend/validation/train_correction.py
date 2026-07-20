#!/usr/bin/env python3
"""
Phase 5a: train the FINAL residual-correction model on the whole feature table and
save it where the backend can load it (app/ml/correction_model.joblib).

This is the same GBT validated by build_model.py / phase4_skill.py, fit on all data
(no CV — CV was for honest evaluation; the shipped model uses everything). The saved
bundle carries the feature spec so the adapter builds an identical feature vector at
inference time. Enable in production with ML_CORRECTION=true.

Usage: python validation/train_correction.py --features validation/features.csv
"""
import argparse
import json
import os
import sys

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold
from sklearn.metrics import r2_score

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NUM = ["month", "log_t0", "momentum", "mean_wind", "peak_wind", "peak_gust",
       "dir_consistency", "temp_c", "rh", "vpd", "hdw", "slope_pct", "horizon_h"]
CAT = ["fuel_model"]
CLIP = (0.3, 3.0)          # correction factor is clamped to this range at inference


def _new_model():
    return HistGradientBoostingRegressor(
        loss="absolute_error", max_depth=3, learning_rate=0.05, max_iter=400,
        min_samples_leaf=20, l2_regularization=1.0,
        categorical_features=[len(NUM)], random_state=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=os.path.join(os.path.dirname(__file__), "features.csv"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "app", "ml",
                                                   "correction_model.joblib"))
    args = ap.parse_args()

    df = pd.read_csv(args.features)
    df = df[(df["residual"].notna()) & (df["pred_km2"] > 0)].copy()
    df = df[np.isfinite(df["residual"])]
    df["log_t0"] = np.log(df["t0_km2"].clip(lower=1))
    df["log_resid"] = np.log(df["residual"].clip(0.25, 4.0))
    X = df[NUM + CAT].copy()
    X[CAT[0]] = X[CAT[0]].astype("category")
    y = df["log_resid"].to_numpy()
    groups = (df["fire"].astype(str) + "|" + df["state"].fillna("") + "|" + df["year"].astype(str)).to_numpy()

    # Honest CV R² for the record (same protocol as build_model).
    oof = np.zeros(len(df))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        oof[te] = _new_model().fit(X.iloc[tr], y[tr]).predict(X.iloc[te])
    cv_r2 = float(r2_score(y, oof))

    model = _new_model().fit(X, y)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    joblib.dump({"model": model, "num": NUM, "cat": CAT, "clip": CLIP}, args.out)

    meta = {"n_rows": int(len(df)), "n_fires": int(pd.Series(groups).nunique()),
            "cv_r2_log_resid": round(cv_r2, 3), "num_features": NUM, "cat_features": CAT,
            "clip": list(CLIP)}
    with open(os.path.splitext(args.out)[0] + "_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"Trained on {len(df)} rows / {meta['n_fires']} fires; CV R2 (log-resid) = {cv_r2:+.3f}")
    print(f"Saved model -> {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
