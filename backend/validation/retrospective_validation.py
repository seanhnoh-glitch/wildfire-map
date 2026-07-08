#!/usr/bin/env python3
"""
Retrospective (hindcast) validation for the ForeFire spread forecast.

Unlike the prospective harness, this needs NO waiting: it replays a PAST window of
a fire using data that already exists.

  - Observed spread comes from the **NASA FIRMS active-fire archive**: we build the
    burned footprint at T0 and at T1 from satellite hotspot detections (a proxy for
    the perimeter — rougher, but always available for any past fire).
  - Wind + humidity come from **Open-Meteo's historical archive** (ERA5).
  - We feed the T0 footprint + the real historical wind/humidity into `/predict`
    (via its hindcast overrides) and score the forecast against the T1 footprint.

Usage (backend must be running; needs FIRMS_MAP_KEY in backend/.env):

    python retrospective_validation.py run \
        --bbox -110.2,37.5,-109.4,38.1 \
        --start 2026-06-20 --t0 2026-06-28 --t1 2026-06-29

  --bbox   W,S,E,N around the fire
  --start  first date to accumulate detections (near the fire's start)
  --t0     forecast start date  (footprint = detections through this day)
  --t1     forecast end date    (footprint = detections through this day)
           horizon = (t1 - t0), must be 1–2 days (schema caps /predict at 48 h)

Scoring is the same as the prospective tool (Jaccard / Dice / area bias vs a
persistence baseline), plus a GeoJSON overlay of T0 / forecast / observed(T1).

CAVEAT: FIRMS footprints are a hotspot proxy, not a mapped perimeter — they tend
to OVER-cover (each detection buffered to a ~375 m pixel) and miss cool/obscured
edges. Read results as directional/extent skill, not a precise match. Free-spread
model limits (suppression, spotting) still apply — see README.md.
"""
import argparse
import csv
import io
import json
import math
import os
import sys
from datetime import date, datetime, timezone

import httpx
from shapely.geometry import Point, mapping
from shapely.ops import transform, unary_union

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/ on path
from prospective_validation import _metrics, _km2, _styled, _to_local  # noqa: E402

API = os.environ.get("WILDFIRE_API", "http://localhost:8000")
_R = 6_371_000.0
_PIXEL_M = 375.0        # VIIRS pixel ~375 m; buffer each detection by this


def _firms_key():
    k = os.environ.get("FIRMS_MAP_KEY")
    if k:
        return k
    try:
        from app.config import get_settings
        return get_settings().firms_map_key
    except Exception:
        return ""


# --- FIRMS archive ---------------------------------------------------------

def _fetch_detections(bbox, start, end, sensor):
    """All (lon, lat, acq_date) detections in bbox over [start, end] (inclusive),
    from the FIRMS area archive (paged in ≤10-day chunks)."""
    key = _firms_key()
    if not key:
        sys.exit("No FIRMS_MAP_KEY (set it in backend/.env or the environment).")
    w, s, e, n = bbox
    out = []
    cur = start
    with httpx.Client(timeout=90.0, headers={"User-Agent": "WildfireMap/0.1"}) as c:
        while cur <= end:
            days = min(5, (end - cur).days + 1)   # FIRMS area API caps day-range at 5
            url = (f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{sensor}/"
                   f"{w},{s},{e},{n}/{days}/{cur.isoformat()}")
            r = c.get(url)
            r.raise_for_status()
            for row in csv.DictReader(io.StringIO(r.text)):
                try:
                    out.append((float(row["longitude"]), float(row["latitude"]), row["acq_date"]))
                except (KeyError, ValueError):
                    continue
            cur = date.fromordinal(cur.toordinal() + days)
    return out


# --- footprint from detections ---------------------------------------------

def _footprint_local(points_lonlat, olat, olon, buffer_m=_PIXEL_M):
    """Union of ~pixel-sized circles around each detection → a burned-area
    footprint, as a shapely geometry in local metres about (olat, olon)."""
    coslat = math.cos(math.radians(olat))
    circles = []
    for lon, lat in points_lonlat:
        dx = math.radians(lon - olon) * _R * coslat
        dy = math.radians(lat - olat) * _R
        circles.append(Point(dx, dy).buffer(buffer_m, quad_segs=6))
    if not circles:
        return None
    fp = unary_union(circles)
    return fp.buffer(buffer_m * 0.6).buffer(-buffer_m * 0.6)   # close small gaps


def _local_to_geojson(geom_local, olat, olon):
    coslat = math.cos(math.radians(olat))

    def _t(xs, ys, zs=None):
        return ([olon + math.degrees(x / (_R * coslat)) for x in xs],
                [olat + math.degrees(y / _R) for y in ys])

    return mapping(transform(_t, geom_local))


# --- Open-Meteo historical -------------------------------------------------

def _fetch_history(lat, lon, t0, t1):
    """Hourly wind + temp/RH for [t0, t1] from Open-Meteo's ERA5 archive.
    Returns (wind_series [[kmh, from_deg], ...], mean_temp_c, mean_rh)."""
    p = {
        "latitude": lat, "longitude": lon,
        "start_date": t0.isoformat(), "end_date": t1.isoformat(),
        "hourly": "wind_speed_10m,wind_direction_10m,temperature_2m,relative_humidity_2m",
        "wind_speed_unit": "kmh", "timezone": "UTC",
    }
    r = httpx.get("https://archive-api.open-meteo.com/v1/archive", params=p, timeout=60.0)
    r.raise_for_status()
    h = r.json().get("hourly", {})
    spd, dr = h.get("wind_speed_10m") or [], h.get("wind_direction_10m") or []
    temp, rh = h.get("temperature_2m") or [], h.get("relative_humidity_2m") or []
    series = [[float(s), float(d)] for s, d in zip(spd, dr) if s is not None and d is not None]
    tvals = [t for t in temp if t is not None]
    rvals = [x for x in rh if x is not None]
    mean_t = sum(tvals) / len(tvals) if tvals else None
    mean_rh = sum(rvals) / len(rvals) if rvals else None
    return series, mean_t, mean_rh


# --- core: one hindcast -> metrics dict -----------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))


def _forecast_and_score(tag, olat, olon, t0_local, t1_local, t0_geo, series,
                        mean_t, mean_rh, horizon_h, overlay, waf_scale, wind_scale,
                        t0lbl, t1lbl):
    """Shared core: send the T0 footprint + wind to /predict, score the forecast
    against the T1 footprint, write the overlay, return the metrics dict. Used by
    both the FIRMS-footprint and GeoMAC-perimeter front-ends."""
    if wind_scale and wind_scale != 1.0:
        series = [[s * wind_scale, d] for s, d in series]
    body = {
        "lat": olat, "lon": olon, "duration_hours": horizon_h, "step_minutes": 60,
        "ignite_from_perimeter": False, "ignition_geojson": t0_geo,
        "wind_series": series, "temperature_c": mean_t, "relative_humidity": mean_rh,
        "waf_scale": waf_scale,
    }
    r = httpx.post(f"{API}/predict", json=body, timeout=300.0)
    r.raise_for_status()
    last = max(r.json()["isochrones"]["features"], key=lambda f: f["properties"]["hours"])
    pred_local = _to_local(last["geometry"], olat, olon)
    m = _metrics(pred_local, t1_local)
    base = _metrics(t0_local, t1_local)
    with open(overlay, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            _styled(t0_geo, f"T0 ({t0lbl})", "#3388ff"),
            _styled(last["geometry"], f"forecast +{horizon_h}h", "#ff8800"),
            _styled(_local_to_geojson(t1_local, olat, olon), f"observed T1 ({t1lbl})", "#d00000"),
        ]}, fh)
    t0_km2 = _km2(t0_local)
    return {
        "label": tag, "horizon_h": horizon_h,
        "t0_km2": t0_km2, "pred_km2": m["pred_km2"], "obs_km2": m["obs_km2"],
        "grew_pct": 100.0 * (m["obs_km2"] - t0_km2) / max(t0_km2, 1e-9),
        "jaccard": m["jaccard"], "dice": m["dice"], "area_bias": m["area_bias"],
        "base_jaccard": base["jaccard"], "skill": m["jaccard"] - base["jaccard"],
        "overlay": overlay,
    }


# --- GeoMAC real daily perimeters (2000-2019) ------------------------------

_GEOMAC = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
           "Historic_Geomac_Perimeters_{year}/FeatureServer/0/query")


def _fetch_geomac(fire, year, t0, t1, state=None):
    """Real mapped perimeters for a past fire from the GeoMAC archive. Returns
    (t0_geojson, t1_geojson) — the largest perimeter on each date. Raises with the
    available dates if either day is missing."""
    where = f"UPPER(incidentname) LIKE '%{fire.upper()}%'"
    if state:
        where += f" AND state='{state.upper()}'"
    params = {"where": where, "outFields": "incidentname,perimeterdatetime,gisacres,state",
              "returnGeometry": "true", "outSR": "4326", "resultRecordCount": 3000, "f": "geojson"}
    r = httpx.get(_GEOMAC.format(year=year), params=params, timeout=90.0)
    r.raise_for_status()
    feats = r.json().get("features", [])

    def _date(f):
        dt = (f.get("properties") or {}).get("perimeterdatetime")
        return datetime.fromtimestamp(dt / 1000, timezone.utc).date() if dt else None

    def pick(target):
        best, ba = None, -1.0
        for f in feats:
            if _date(f) == target:
                a = (f["properties"].get("gisacres") or 0)
                if a > ba and f.get("geometry"):
                    ba, best = a, f
        return best

    f0, f1 = pick(t0), pick(t1)
    if not f0 or not f1:
        avail = sorted({_date(f).isoformat() for f in feats if _date(f)})
        raise ValueError(f"missing perimeter ({'T0 ' + str(t0) if not f0 else 'T1 ' + str(t1)}); "
                         f"available dates: {avail[:12]}")
    return f0["geometry"], f1["geometry"]


def run_perimeter(fire, year, t0, t1, overlay=None, verbose=True, label=None,
                  waf_scale=None, wind_scale=None, state=None):
    """Hindcast a past fire day against REAL GeoMAC perimeters (no FIRMS proxy)."""
    from shapely.geometry import shape
    t0d, t1d = (date.fromisoformat(str(d)) for d in (t0, t1))
    if not (1 <= (t1d - t0d).days <= 2):
        raise ValueError("t1 must be 1-2 days after t0 (horizon caps at 48 h)")
    horizon_h = (t1d - t0d).days * 24
    tag = label or f"{fire} {t0}->{t1}"
    if verbose:
        print(f"[{tag}] GeoMAC {year} perimeters ...")
    t0_geo, t1_geo = _fetch_geomac(fire, year, t0d, t1d, state)
    g1 = shape(t1_geo)
    if not g1.is_valid:
        g1 = g1.buffer(0)
    olat, olon = g1.centroid.y, g1.centroid.x
    t0_local = _to_local(t0_geo, olat, olon)
    t1_local = _to_local(t1_geo, olat, olon)
    series, mean_t, mean_rh = _fetch_history(olat, olon, t0d, t1d)
    if not series:
        raise ValueError("no historical wind from Open-Meteo")
    out = overlay or os.path.join(_HERE, f"hindcast_perim_{fire}_{t0}_{t1}_overlay.geojson")
    return _forecast_and_score(tag, olat, olon, t0_local, t1_local, t0_geo, series,
                               mean_t, mean_rh, horizon_h, out, waf_scale, wind_scale, t0, t1)


def run_hindcast(bbox, start, t0, t1, sensor="VIIRS_NOAA20_NRT",
                 overlay=None, verbose=True, label=None, waf_scale=None, wind_scale=None):
    """Hindcast one window and return a metrics dict. Raises ValueError on data
    problems (too few detections, no weather). waf_scale (if set) multiplies the
    backend's wind adjustment factor; wind_scale multiplies the wind series we
    send (an empirical spread-reduction / suppression calibration, testable
    without a backend change)."""
    if isinstance(bbox, str):
        bbox = [float(x) for x in bbox.split(",")]
    start, t0, t1 = (date.fromisoformat(str(d)) for d in (start, t0, t1))
    if not (1 <= (t1 - t0).days <= 2):
        raise ValueError("t1 must be 1-2 days after t0 (horizon caps at 48 h)")
    horizon_h = (t1 - t0).days * 24
    tag = label or f"{t0}->{t1}"

    if verbose:
        print(f"[{tag}] FIRMS {sensor} {start}..{t1} over {bbox} ...")
    det = _fetch_detections(bbox, start, t1, sensor)
    d_t0 = [(lon, lat) for lon, lat, d in det if d <= t0.isoformat()]
    d_t1 = [(lon, lat) for lon, lat, d in det if d <= t1.isoformat()]
    if len(d_t0) < 3 or len(d_t1) < 3:
        raise ValueError(f"too few detections (T0={len(d_t0)}, T1={len(d_t1)})")

    olon = sum(p[0] for p in d_t1) / len(d_t1)
    olat = sum(p[1] for p in d_t1) / len(d_t1)
    t0_local = _footprint_local(d_t0, olat, olon)
    t1_local = _footprint_local(d_t1, olat, olon)
    t0_geo = _local_to_geojson(t0_local, olat, olon)

    series, mean_t, mean_rh = _fetch_history(olat, olon, t0, t1)
    if not series:
        raise ValueError("no historical wind from Open-Meteo")
    if verbose:
        wx = f", mean {mean_t:.0f}°C / {mean_rh:.0f}% RH" if mean_t is not None else ""
        print(f"[{tag}] detections T0={len(d_t0)} T1={len(d_t1)}; {len(series)} h wind{wx}; "
              f"forecasting {horizon_h} h ...")
    out = overlay or os.path.join(_HERE, f"hindcast_{t0}_{t1}_overlay.geojson")
    return _forecast_and_score(tag, olat, olon, t0_local, t1_local, t0_geo, series,
                               mean_t, mean_rh, horizon_h, out, waf_scale, wind_scale, t0, t1)


# A run is a FAIR test of the forecast only if the fire is big enough for the
# FIRMS hotspot proxy to be meaningful, AND it actually moved enough that
# "assume no change" isn't already near-perfect (else persistence is unbeatable).
_MIN_OBS_KM2 = 50.0
_MAX_BASE_JACCARD = 0.85


def _is_fair(r):
    return r["obs_km2"] >= _MIN_OBS_KM2 and r["base_jaccard"] <= _MAX_BASE_JACCARD


def _unfair_reason(r):
    if r["obs_km2"] < _MIN_OBS_KM2:
        return "too small"
    if r["base_jaccard"] > _MAX_BASE_JACCARD:
        return "quiet day"
    return ""


def _print_single(res):
    print(f"\nAreas (km²):  T0 {res['t0_km2']:8.2f}   forecast {res['pred_km2']:8.2f}   "
          f"observed(T1) {res['obs_km2']:8.2f}   (fire grew {res['grew_pct']:+.0f}%)")
    print(f"\n{'metric':<14}{'FORECAST vs observed':>22}{'  persistence (T0) baseline':>28}")
    print(f"{'Jaccard':<14}{res['jaccard']:>22.3f}{res['base_jaccard']:>28.3f}")
    print(f"{'Dice':<14}{res['dice']:>22.3f}")
    print(f"{'area bias':<14}{res['area_bias']:>22.2f}")
    verdict = ("adds skill over persistence" if res["skill"] > 0.01 else
               "no better than 'no change'" if res["skill"] > -0.01 else "worse than persistence")
    print(f"\nForecast Jaccard - baseline = {res['skill']:+.3f}  -> {verdict}")
    if not _is_fair(res):
        print(f"NOTE: not a fair test ({_unfair_reason(res)}) — take this result with a grain of salt.")
    print(f"\nOverlay written: {res['overlay']}  (open in https://geojson.io)")


def cmd_run(args):
    _print_single(run_hindcast(args.bbox, args.start, args.t0, args.t1, args.sensor,
                               args.overlay, waf_scale=args.waf_scale, wind_scale=args.wind_scale))


def cmd_perimeter(args):
    _print_single(run_perimeter(args.fire, args.year, args.t0, args.t1, args.overlay,
                                state=args.state, waf_scale=args.waf_scale, wind_scale=args.wind_scale))


def cmd_batch(args):
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    runs = cfg["runs"] if isinstance(cfg, dict) else cfg
    default_sensor = cfg.get("sensor", "VIIRS_NOAA20_NRT") if isinstance(cfg, dict) else "VIIRS_NOAA20_NRT"

    print(f"Running {len(runs)} hindcasts (this can take a few minutes — a ForeFire run each) ...\n")
    results = []
    for run in runs:
        label = run.get("label") or f"{run.get('fire', '')} {run['t0']}->{run['t1']}".strip()
        try:
            if "fire" in run:   # GeoMAC real-perimeter run
                res = run_perimeter(run["fire"], run["year"], run["t0"], run["t1"],
                                    verbose=False, label=label, state=run.get("state"),
                                    waf_scale=run.get("waf_scale", args.waf_scale),
                                    wind_scale=run.get("wind_scale", args.wind_scale))
            else:               # FIRMS footprint run
                res = run_hindcast(run["bbox"], run["start"], run["t0"], run["t1"],
                                   run.get("sensor", default_sensor), verbose=False, label=label,
                                   waf_scale=run.get("waf_scale", args.waf_scale),
                                   wind_scale=run.get("wind_scale", args.wind_scale))
            results.append(res)
            print(f"  ok   {label:<26} Jacc {res['jaccard']:.3f}  skill {res['skill']:+.3f}  bias {res['area_bias']:.2f}")
        except Exception as e:
            print(f"  ERR  {label:<26} {type(e).__name__}: {str(e)[:60]}")

    if not results:
        print("\nNo successful runs.")
        return
    W = 100
    print("\n" + "=" * W)
    print(f"{'run':<26}{'T0':>6}{'fcst':>6}{'obs':>6}{'grew%':>7}{'Jacc':>7}{'base':>7}{'skill':>8}{'bias':>7}{'  fair'}")
    print("-" * W)
    for r in results:
        fair = "yes" if _is_fair(r) else f"no ({_unfair_reason(r)})"
        print(f"{r['label'][:26]:<26}{r['t0_km2']:>6.0f}{r['pred_km2']:>6.0f}{r['obs_km2']:>6.0f}"
              f"{r['grew_pct']:>+7.0f}{r['jaccard']:>7.3f}{r['base_jaccard']:>7.3f}"
              f"{r['skill']:>+8.3f}{r['area_bias']:>7.2f}  {fair}")
    print("-" * W)

    fair = [r for r in results if _is_fair(r)]
    if fair:
        nf = len(fair)
        print(f"{'MEAN (fair tests, ' + str(nf) + ')':<45}"
              f"{sum(r['grew_pct'] for r in fair)/nf:>+7.0f}{sum(r['jaccard'] for r in fair)/nf:>7.3f}"
              f"{sum(r['base_jaccard'] for r in fair)/nf:>7.3f}{sum(r['skill'] for r in fair)/nf:>+8.3f}"
              f"{sum(r['area_bias'] for r in fair)/nf:>7.2f}")
        pos = sum(1 for r in fair if r["skill"] > 0.01)
        print("=" * W)
        print(f"{pos}/{nf} FAIR tests beat persistence.  ({len(results) - nf} unfair runs excluded "
              f"from the mean.)  bias>1 over-predicts, <1 under-predicts.")
    else:
        print("=" * W)
        print("No fair tests (all too small or quiet days). Pick larger fires on active-growth days.")


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="hindcast one past fire window and score it")
    r.add_argument("--bbox", required=True, help="W,S,E,N")
    r.add_argument("--start", required=True, help="YYYY-MM-DD, first day of detections")
    r.add_argument("--t0", required=True, help="YYYY-MM-DD, forecast start day")
    r.add_argument("--t1", required=True, help="YYYY-MM-DD, forecast end day (1-2 days after t0)")
    r.add_argument("--sensor", default="VIIRS_NOAA20_NRT",
                   help="FIRMS source (…_NRT recent, …_SP older archive)")
    r.add_argument("--overlay", default=None)
    r.add_argument("--waf-scale", type=float, default=None,
                   help="multiply the wind adjustment factor (WAF experiment; needs backend rebuilt)")
    r.add_argument("--wind-scale", type=float, default=None,
                   help="multiply the wind series sent (spread/suppression calibration; no rebuild)")
    r.set_defaults(func=cmd_run)

    pm = sub.add_parser("perimeter", help="hindcast a past fire vs REAL GeoMAC perimeters (2000-2019)")
    pm.add_argument("--fire", required=True, help="incident name (substring, e.g. CARR)")
    pm.add_argument("--year", required=True, type=int, help="fire year, 2000-2019")
    pm.add_argument("--t0", required=True, help="YYYY-MM-DD, forecast start day")
    pm.add_argument("--t1", required=True, help="YYYY-MM-DD, forecast end day (1-2 days after t0)")
    pm.add_argument("--state", default=None, help="2-letter state to disambiguate (e.g. CA)")
    pm.add_argument("--overlay", default=None)
    pm.add_argument("--waf-scale", type=float, default=None)
    pm.add_argument("--wind-scale", type=float, default=None,
                    help="multiply the wind series sent (spread calibration; no rebuild)")
    pm.set_defaults(func=cmd_perimeter)

    b = sub.add_parser("batch", help="run many hindcasts from a JSON config and print a summary table")
    b.add_argument("--config", required=True,
                   help="JSON runs: FIRMS {label,bbox,start,t0,t1} or GeoMAC {label,fire,year,t0,t1,state}")
    b.add_argument("--waf-scale", type=float, default=None,
                   help="multiply the WAF for all runs (per-run 'waf_scale' in config overrides)")
    b.add_argument("--wind-scale", type=float, default=None,
                   help="multiply the wind series for all runs (spread calibration; no rebuild)")
    b.set_defaults(func=cmd_batch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
