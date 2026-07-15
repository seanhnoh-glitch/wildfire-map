#!/usr/bin/env python3
"""
Harvest a large fire-day dataset from the GeoMAC archive (2000-2019) — Phase 1 of
the ML residual-correction plan (see README).

For every fire in each year's GeoMAC perimeter service, it groups the mapped
perimeters by day, then emits every consecutive daily pair (T0 -> T1, 1-2 days
apart) whose T1 footprint is big enough to score. Each pair is one training/
validation example. Fire identity (name + state + year) is kept so the eventual
model can be split BY FIRE, not by day, to avoid leakage.

Output: a batch manifest the existing `retrospective_validation.py batch` harness
can run directly (validation/batch_harvested.json), plus a coverage summary.
Per-year perimeter metadata is cached (validation/snapshots/geomac_meta/) so
re-runs are fast.

Usage:
    python validation/harvest.py --years 2011-2019 --min-acres 2000
    python validation/harvest.py --years 2000-2019 --min-acres 1000 --max-per-fire 4
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_HERE, "snapshots", "geomac_meta")
_GEOMAC = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
           "Historic_Geomac_Perimeters_{year}/FeatureServer/0/query")
_ACRE_KM2 = 0.00404686


def _fetch_year(year, verbose=True):
    """All (name, state, epoch_ms, acres) perimeter records for a year, paged and
    cached to disk (paging every year is slow)."""
    cf = os.path.join(_CACHE, f"{year}.json")
    if os.path.exists(cf):
        try:
            with open(cf, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    u = _GEOMAC.format(year=year)
    recs, offset, page = [], 0, 2000
    try:
        with httpx.Client(timeout=90.0, headers={"User-Agent": "WildfireMap/0.1"}) as c:
            while True:
                r = c.get(u, params={
                    "where": "1=1",
                    "outFields": "incidentname,perimeterdatetime,gisacres,state",
                    "returnGeometry": "false", "resultOffset": offset,
                    "resultRecordCount": page, "orderByFields": "OBJECTID", "f": "json"})
                r.raise_for_status()
                j = r.json()
                feats = j.get("features", [])
                for f in feats:
                    a = f.get("attributes") or {}
                    nm, dt, ac, st = (a.get("incidentname"), a.get("perimeterdatetime"),
                                      a.get("gisacres"), a.get("state"))
                    if nm and dt and ac:
                        recs.append([nm, st or "", int(dt), float(ac)])
                if verbose:
                    print(f"  {year}: {len(recs)} records…", end="\r")
                if not j.get("exceededTransferLimit") or not feats:
                    break
                offset += len(feats)
    except Exception as e:
        print(f"  {year}: FETCH ERROR {str(e)[:80]}")
        return []
    os.makedirs(_CACHE, exist_ok=True)
    with open(cf, "w", encoding="utf-8") as fh:
        json.dump(recs, fh)
    if verbose:
        print(f"  {year}: {len(recs)} perimeter records (cached)          ")
    return recs


def _fire_days(recs, year, min_acres, max_gap, max_per_fire):
    """Group a year's records into fires → {date: largest acres}, then emit
    consecutive daily pairs (T0 → T1)."""
    fires = defaultdict(dict)                       # (name, state) -> {date: acres}
    for nm, st, dt, ac in recs:
        # GeoMAC has some junk timestamps (0, negative, far-future). Keep only sane ones.
        if not (9.0e11 < dt < 1.6e12):              # ~1998-08 .. 2020-09 in ms
            continue
        try:
            d = datetime.fromtimestamp(dt / 1000, timezone.utc).date()
        except (OSError, ValueError, OverflowError):
            continue
        key = (nm.strip().upper(), st)
        fires[key][d] = max(fires[key].get(d, 0.0), ac)

    out = []
    for (nm, st), daymap in fires.items():
        days = sorted(daymap)
        pairs = []
        for i in range(len(days) - 1):
            t0, t1 = days[i], days[i + 1]
            gap = (t1 - t0).days
            if not (1 <= gap <= max_gap):
                continue
            a0, a1 = daymap[t0], daymap[t1]
            if a1 < min_acres:                      # T1 (observed) too small to score
                continue
            pairs.append((t0, t1, a0, a1))
        # Keep the biggest-growth pairs per fire so one huge fire can't flood the set.
        pairs.sort(key=lambda p: (p[3] - p[2]), reverse=True)
        for t0, t1, a0, a1 in pairs[:max_per_fire]:
            grew = 100.0 * (a1 - a0) / max(a0, 1e-9)
            out.append({
                "label": f"{nm[:22]} {t0.isoformat()}",
                "fire": nm, "year": year, "state": st,
                "t0": t0.isoformat(), "t1": t1.isoformat(),
                "t0_acres": round(a0, 1), "t1_acres": round(a1, 1),
                "t1_km2": round(a1 * _ACRE_KM2, 1), "grew_pct": round(grew, 1),
            })
    return out


def _summary(examples):
    n = len(examples)
    fires = {(e["fire"], e["state"], e["year"]) for e in examples}
    run = sum(1 for e in examples if e["grew_pct"] >= 100)
    mod = sum(1 for e in examples if 10 <= e["grew_pct"] < 100)
    quiet = sum(1 for e in examples if e["grew_pct"] < 10)
    by_year = defaultdict(int)
    by_state = defaultdict(int)
    for e in examples:
        by_year[e["year"]] += 1
        by_state[e["state"]] += 1
    print("\n" + "=" * 66)
    print(f"HARVESTED {n} fire-day examples from {len(fires)} distinct fires")
    print("-" * 66)
    print(f"  by behaviour:  run days (grew ≥100%) {run:>5}   "
          f"moderate (10-100%) {mod:>5}   quiet (<10%) {quiet:>5}")
    yr = "  ".join(f"{y}:{by_year[y]}" for y in sorted(by_year))
    print(f"  by year:       {yr}")
    top = sorted(by_state.items(), key=lambda kv: -kv[1])[:12]
    print(f"  top states:    " + "  ".join(f"{s or '?'}:{c}" for s, c in top))
    sizes = sorted(e["t1_km2"] for e in examples)
    if sizes:
        med = sizes[len(sizes) // 2]
        print(f"  T1 size (km²): min {sizes[0]:.0f}  median {med:.0f}  max {sizes[-1]:.0f}")
    print("=" * 66)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", default="2011-2019", help="e.g. 2011-2019 or 2013,2015,2018")
    p.add_argument("--min-acres", type=float, default=2000.0,
                   help="min T1 (observed) size to score meaningfully (~2000 ac ≈ 8 km²)")
    p.add_argument("--max-gap", type=int, default=2, help="max days between T0 and T1 (/predict caps at 48h)")
    p.add_argument("--max-per-fire", type=int, default=6, help="cap examples per fire (keeps the biggest-growth days)")
    p.add_argument("--out", default=os.path.join(_HERE, "batch_harvested.json"))
    args = p.parse_args()

    if "-" in args.years and "," not in args.years:
        a, b = args.years.split("-")
        years = list(range(int(a), int(b) + 1))
    else:
        years = [int(y) for y in args.years.split(",")]

    print(f"Harvesting GeoMAC {years[0]}–{years[-1]} (min T1 {args.min_acres:.0f} ac, "
          f"gap ≤{args.max_gap}d, ≤{args.max_per_fire}/fire) …")
    examples = []
    for y in years:
        recs = _fetch_year(y)
        examples += _fire_days(recs, y, args.min_acres, args.max_gap, args.max_per_fire)

    examples.sort(key=lambda e: (e["year"], e["fire"], e["t0"]))
    _summary(examples)
    manifest = {
        "_comment": (f"Auto-harvested by harvest.py from GeoMAC {years[0]}-{years[-1]} "
                     f"(min T1 {args.min_acres:.0f} ac, gap ≤{args.max_gap}d, ≤{args.max_per_fire}/fire). "
                     f"{len(examples)} fire-day examples. Run with retrospective_validation.py batch."),
        "runs": examples,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=0)
    print(f"\nWrote {len(examples)} examples → {args.out}")
    print("Next: run a subset through the batch harness to produce ForeFire predictions +\n"
          "residuals (Phase 2). Split BY FIRE for any train/test evaluation.")


if __name__ == "__main__":
    main()
