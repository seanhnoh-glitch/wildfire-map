#!/usr/bin/env python3
"""
Phase 3: train a residual-correction model on the harvested feature table and test,
with BY-FIRE cross-validation, whether it actually helps.

Target = log(observed_area / forecast_area) — the multiplicative correction that
would fix ForeFire's magnitude. We compare, on held-out fires:

  * no correction      — the physics model as-is
  * global recalibration — multiply every forecast by the train-set median residual
                           (a "dumb" constant damp/boost, no ML)
  * GBT (this model)   — a gradient-boosted tree using forecast-time features

The question ML has to answer: does using the FEATURES beat a single global rescale?
If GBT ≈ global recalibration, the features carry no extra signal and ML isn't worth
it — just recalibrate. If GBT clearly wins on held-out fires, ML earns its place.

Usage:  python validation/build_model.py --features validation/features.csv
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold
from sklearn.inspection import permutation_importance

NUM = ["month", "log_t0", "momentum", "mean_wind", "peak_wind", "peak_gust",
       "dir_consistency", "temp_c", "rh", "vpd", "hdw", "slope_pct", "horizon_h"]
CAT = ["fuel_model"]


def _bias_err(pred_km2, obs_km2):
    """Mean absolute magnitude error |predicted/observed - 1| (0 = perfect area)."""
    return float(np.mean(np.abs(pred_km2 / obs_km2 - 1.0)))


def _stratified_recal(by_vals, residual, pred_km2, groups, gkf, n_bins=3):
    """Regime-aware recalibration: the correction factor is the TRAIN median residual
    within the fire's bin of a forecast-time signal `by_vals` (e.g. momentum, HDW).
    Damps the bins that over-predict and leaves/boosts those that don't — the
    hand-crafted version of what the GBT tried to learn. Held-out corrected areas."""
    v = np.asarray(by_vals, float)
    v = np.where(np.isnan(v), np.nanmedian(v), v)
    out = pred_km2.astype(float).copy()
    for tr, te in gkf.split(v, residual, groups):
        edges = np.quantile(v[tr], np.linspace(0, 1, n_bins + 1))
        edges[0], edges[-1] = -np.inf, np.inf
        tr_bin = np.clip(np.digitize(v[tr], edges[1:-1]), 0, n_bins - 1)
        te_bin = np.clip(np.digitize(v[te], edges[1:-1]), 0, n_bins - 1)
        gmed = np.median(residual[tr])
        fac = {b: (np.median(residual[tr][tr_bin == b]) if (tr_bin == b).any() else gmed)
               for b in range(n_bins)}
        out[te] = pred_km2[te] * np.array([fac[b] for b in te_bin])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=os.path.join(os.path.dirname(__file__), "features.csv"))
    ap.add_argument("--folds", type=int, default=5)
    args = ap.parse_args()

    df = pd.read_csv(args.features)
    df = df[(df["residual"].notna()) & (df["pred_km2"] > 0) & (df["obs_km2"] > 0)].copy()
    df = df[np.isfinite(df["residual"])]
    n = len(df)
    fires = (df["fire"].astype(str) + "|" + df["state"].fillna("") + "|" + df["year"].astype(str))
    print(f"{n} rows from {fires.nunique()} distinct fires "
          f"({args.folds}-fold by-fire CV)\n")
    if n < 60 or fires.nunique() < args.folds:
        print("Too few rows/fires for a trustworthy model — collect more first.")
        return

    df["log_t0"] = np.log(df["t0_km2"].clip(lower=1))
    df["log_resid"] = np.log(df["residual"].clip(0.25, 4.0))
    X = df[NUM + CAT].copy()
    X[CAT[0]] = X[CAT[0]].astype("category")
    y = df["log_resid"].to_numpy()
    pred_km2 = df["pred_km2"].to_numpy()
    obs_km2 = df["obs_km2"].to_numpy()
    groups = fires.to_numpy()

    gkf = GroupKFold(n_splits=args.folds)
    gbt_log = np.zeros(n)
    const_fac = np.zeros(n)
    for tr, te in gkf.split(X, y, groups):
        model = HistGradientBoostingRegressor(
            loss="absolute_error", max_depth=3, learning_rate=0.05, max_iter=400,
            min_samples_leaf=20, l2_regularization=1.0,
            categorical_features=[len(NUM)], random_state=0)
        model.fit(X.iloc[tr], y[tr])
        gbt_log[te] = model.predict(X.iloc[te])
        const_fac[te] = np.median(df["residual"].to_numpy()[tr])   # global recal from train only

    # Held-out corrected magnitudes.
    pred_gbt = pred_km2 * np.exp(gbt_log)
    pred_const = pred_km2 * const_fac

    from sklearn.metrics import r2_score, mean_absolute_error
    r2 = r2_score(y, gbt_log)
    mae_gbt = mean_absolute_error(y, gbt_log)
    mae_base = mean_absolute_error(y, np.full(n, np.median(y)))   # predict-the-median baseline

    print("=== Can the features predict ForeFire's residual? (held-out) ===")
    print(f"  GBT   R2 on log-residual : {r2:+.3f}   (>0 means features carry signal)")
    print(f"  GBT   MAE on log-residual: {mae_gbt:.3f}")
    print(f"  const MAE (predict median): {mae_base:.3f}   (GBT should be lower)\n")

    # Regime-aware (stratified) recalibrations keyed to a single forecast-time signal.
    resid = df["residual"].to_numpy()
    pred_mom = _stratified_recal(df["momentum"].to_numpy(), resid, pred_km2, groups, gkf)
    pred_hdw = _stratified_recal(df["hdw"].to_numpy(), resid, pred_km2, groups, gkf)

    print("=== Magnitude (area) error |pred/obs - 1|, held-out (lower is better) ===")
    print(f"  no correction (physics)      : {_bias_err(pred_km2, obs_km2):.3f}")
    print(f"  global recalibration (flat)  : {_bias_err(pred_const, obs_km2):.3f}")
    print(f"  regime-aware by MOMENTUM     : {_bias_err(pred_mom, obs_km2):.3f}")
    print(f"  regime-aware by HDW          : {_bias_err(pred_hdw, obs_km2):.3f}")
    print(f"  GBT (all features)           : {_bias_err(pred_gbt, obs_km2):.3f}")
    print(f"  mean area_bias  physics {np.mean(pred_km2/obs_km2):.2f} -> "
          f"flat {np.mean(pred_const/obs_km2):.2f} -> momentum {np.mean(pred_mom/obs_km2):.2f} "
          f"-> GBT {np.mean(pred_gbt/obs_km2):.2f}  (1.0=unbiased)\n")

    # Does ANY forecast-time signal correlate with how wrong ForeFire is?
    from scipy.stats import spearmanr
    print("=== Correlation of each signal with log-residual (Spearman) ===")
    cors = []
    for c in NUM:
        rho, _ = spearmanr(df[c].to_numpy(), df["log_resid"].to_numpy(), nan_policy="omit")
        cors.append((c, rho))
    for c, rho in sorted(cors, key=lambda kv: -abs(kv[1] if kv[1] == kv[1] else 0))[:8]:
        print(f"  {c:16} rho={rho:+.3f}")
    print()

    # Feature importance on a full-data fit (for interpretation only).
    full = HistGradientBoostingRegressor(
        loss="absolute_error", max_depth=3, learning_rate=0.05, max_iter=400,
        min_samples_leaf=20, l2_regularization=1.0,
        categorical_features=[len(NUM)], random_state=0).fit(X, y)
    imp = permutation_importance(full, X, y, n_repeats=8, random_state=0)
    order = np.argsort(imp.importances_mean)[::-1]
    print("=== Top features (permutation importance) ===")
    cols = NUM + CAT
    for i in order[:8]:
        print(f"  {cols[i]:16} {imp.importances_mean[i]:+.4f}")


if __name__ == "__main__":
    main()
