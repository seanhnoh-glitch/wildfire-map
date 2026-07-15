# Forecast validation

Two harnesses to check whether the ForeFire forecast actually predicts where a fire
goes:

- **`prospective_validation.py`** — snapshot a forecast for an *active* fire now,
  score it against the fire's real perimeter a day or two later. Uses the live
  pipeline unchanged, but you have to **wait** for NIFC to re-map the perimeter.
- **`retrospective_validation.py`** — hindcast a *past* window with data that
  already exists (**no waiting**): reconstruct the burned footprint at T0 and T1
  from the **FIRMS active-fire archive**, pull the real **historical wind/humidity**
  from Open-Meteo's ERA5 archive, feed the T0 footprint + that weather into
  `/predict` (via its hindcast overrides), and score against the T1 footprint.

Both report the same metrics (below). The retrospective tool is the faster path to
a real number; the prospective tool is more authoritative (real mapped perimeters,
not a hotspot proxy).

## Retrospective (hindcast) — quick start

Needs the backend **rebuilt** with the hindcast overrides (`ignition_geojson`,
`wind_series`, `temperature_c`, `relative_humidity` on `/predict`) and a
`FIRMS_MAP_KEY`. Pick a window where the fire is already established at T0:

```bash
cd backend
./.venv/Scripts/python validation/retrospective_validation.py run \
    --bbox -110.2,37.5,-109.4,38.1 \
    --start 2026-06-22 --t0 2026-06-29 --t1 2026-06-30
```

`--start` accumulates detections from near the fire's start; `--t0`/`--t1` are the
forecast window (1–2 days). Use `--sensor VIIRS_NOAA20_SP` for fires older than ~2
months (NRT only retains recent data). Open-Meteo's ERA5 archive lags ~5 days, so
keep `--t1` at least 5 days in the past. Footprints are a **hotspot proxy** (each
detection buffered to a ~375 m pixel) — rougher than a mapped perimeter and prone
to over-cover; read the result as directional/extent skill.

### Two ground truths — and why FIRMS misled the calibration

**FIRMS footprints (`batch_example.json`, Utah/June 2026).** Against the hotspot
proxy, the raw model looked like it **over-predicts ~1.5×**, and scaling the wind
to ~0.5 "centred" it. That suggested a `spread_wind_adjust = 0.5` default.

**Real GeoMAC perimeters (`batch_geomac.json`, CA/AZ/CO/OR, 2011–2018).** Against
*real mapped perimeters* the story flips: the raw model **under-predicts
active-growth days** (mean area bias ~0.54 across 7 fair fires — all famous fires'
run days) while still **beating persistence 7/7**, and **over-predicts** the one
quiet day. That's the normal free-spread variance, roughly centred — **no
systematic over-prediction.**

The reconciliation: **FIRMS hotspot footprints under-represent the true burned
area** (they miss cool interiors / obscured pixels), so the raw forecast sits
*between* the small FIRMS footprint (looks like over-prediction) and the larger
real perimeter (under-prediction). The FIRMS-based 0.5 was a **proxy artifact**.

**So the default is `config.spread_wind_adjust = 1.0` (raw).** Calibrating down to
0.5 would make the model badly under-predict real perimeters. Lesson: validate
calibrations against **real perimeters**, not a hotspot proxy. (`waf_scale`
per-request and `SPREAD_WIND_ADJUST` env still let you experiment.)

### GeoMAC (real-perimeter) validation

```bash
python validation/retrospective_validation.py perimeter --fire CARR --year 2018 --state CA --t0 2018-07-25 --t1 2018-07-26
python validation/retrospective_validation.py batch --config validation/batch_geomac.json
```

`perimeter`/GeoMAC uses real daily mapped perimeters (2000–2019, any region) as
both T0 and the observed T1 — no proxy. Config runs use `{fire,year,t0,t1,state}`
instead of `{bbox,start,t0,t1}`.

### Batch (a trend across many windows/fires)

Put several windows in a JSON config (see `batch_example.json`) and run them all
into one summary table:

```bash
./.venv/Scripts/python validation/retrospective_validation.py batch --config validation/batch_example.json
```

Config: `{"sensor": "...", "runs": [{"label","bbox","start","t0","t1"}, ...]}`
(each run may override `sensor`; `bbox` may be a list or "W,S,E,N" string). Runs
whose weather isn't in the ERA5 archive yet just show `ERR` — re-run later. The
table prints per-run Jaccard / persistence-baseline / skill / area-bias plus the
means, so you can see whether skill is consistently positive and which way the
area bias leans across fires.

### Reproducible runs (needed to measure a code change)

The external services this harness depends on (GeoMAC perimeters, Open-Meteo ERA5,
LANDFIRE fuels, the DEM) are flaky: a transient timeout silently falls back to
different inputs, and the score then swings ±0.04 skill **on the same config** —
as large as the effects worth chasing. So the inputs are cached:

- **Harness side (automatic):** GeoMAC perimeters and ERA5 wind/humidity are
  snapshotted to `validation/snapshots/inputs/` on first fetch and replayed after.
- **Server side (opt-in):** run the backend with `FETCH_CACHE_DIR=/cache` and a
  mounted volume so the fuel grid, DEM grid, and point slope are cached too:

  ```bash
  docker run -p 8000:8000 --env-file .env \
      -e FETCH_CACHE_DIR=/cache -v "$PWD/.fetch_cache:/cache" wildfire-map-backend
  ```

Once populated, re-running a batch replays byte-identical inputs, so a metric delta
is the *code change*, not network noise. Delete a snapshot / cache file to refetch.
To A/B a model option, toggle it via env (`-e CROWN_SPOTTING=true`,
`-e TERRAIN_DEM=true`) between two otherwise-identical cached runs.

### What the validation has actually shown (2026-07)

Across ~10 fair real-perimeter fires the *raw* model sits at the **free-spread
floor** (mean Jaccard ~0.45, skill ~+0.03, area bias ~0.6 — it under-predicts
growth days). Levers tested:

- **Spread multiplier (`waf_scale` 1→2.8):** moves bias toward 1.0 but *lowers*
  skill — extra spread overshoots downwind in the wrong shape. Kept at 1.0.
- **Real DEM terrain (`TERRAIN_DEM`):** more correct, but no accuracy gain (the
  crude tilted plane's uniform upslope push happens to offset under-prediction).
  Off by default.
- **Seasonal live-fuel moisture:** physically better; roughly neutral on score.

**Root cause found:** the ERA5 point winds fed to these megafire *run days* are
almost all **2–16 km/h**, when the real winds were 40–80 km/h with gusts — the
hourly-mean reanalysis smooths the wind away. That one fact explained both the
under-prediction and why a surface model can't reach the real extents.

**The fix (shipped as the default):** two changes that only work as a *pair* —

- **Gust-blended wind** (`wind_gust_factor`, default 0.5): drive the fire on
  `sustained + 0.5·(gust − sustained)` instead of the smoothed mean. In the harness,
  `--gust-factor` blends the ERA5 gust column (cached in the snapshots).
- **Crown-fire spotting** (`CROWN_SPOTTING`, default on): a downwind ember *fan*
  (services/spotting.py) that the stronger gust wind now actually triggers.

Gusts alone overshoot downwind (skill drops); spotting alone barely fires (the mean
wind never crosses its threshold). Together, the gusts supply the energy and
spotting spreads it into a lateral fan, so on the fair-10 set:

| config | Jaccard | skill | area bias |
|---|---|---|---|
| baseline (sustained wind, no spotting) | 0.468 | +0.039 | 0.72 |
| gusts 0.5 only | 0.460 | +0.031 | 0.93 |
| **gusts 0.5 + spotting (default)** | **0.471** | **+0.042** | **0.99** |

The headline is the **area bias centred at ~1.0** — the systematic under-prediction
is gone (the biggest under-predictor, Thomas 12/5, went from skill +0.07 to +0.27).
Reproduce with a bare `batch` (defaults to gusts 0.5); `--gust-factor 0` plus
`CROWN_SPOTTING=false` recovers the old baseline.

**But normal fires exposed the other half.** Lowering the fair-size floor to 15 km²
and adding moderate-growth days (a normal fire, not just megafire run days) showed
that a *fixed* gust+spotting default **over-predicts calm/humid days** (Soberanes
7/24, +30 % growth, came out at bias ~4). On a 13-fire mixed set: skill −0.019,
bias 1.24.

**Regime-scaled aggressiveness (`regime_scaling`, default on) fixed it.** A
Hot-Dry-Windy index (VPD × wind) cleanly separates the regimes — the worst
over-predictor had HDW ~7, well-calibrated run days ~40–56 — so we damp the driving
wind (×`regime_wind_min`=0.6 at the calm extreme → ×1.0 hot-dry-windy) and the
spotting reach by that index:

| 13-fire mixed set | skill | area bias | beats persistence |
|---|---|---|---|
| gusts + spotting, fixed | −0.019 | 1.24 | 7/13 |
| **+ regime scaling (default)** | **−0.004** | **0.99** | **9/13** |

Run days stayed strong (Thomas 12/5 +0.24, Wallow +0.20) while the over-predictors
came down. Residual over-prediction on genuinely quiet days is the free-spread
model's suppression blind spot (crews/lines it can't see), not a wind problem.

**Spatially-varying wind — investigated, not built.** Reading ForeFire's source and
testing directly: it *does* sample wind per-node (`getNormalWind` →
`windULayer->getValueAt(node)`), and a spatial field steers the fire. But it copies
the field at layer creation and only updates wind globally via `trigger` (location
ignored) or the binary `loadMultiWindBin` coupling path. So the only tractable
spatial wind here is **static**, which sacrifices the temporal evolution that
matters more over 24 h. True spatiotemporal wind needs ForeFire's atmospheric-model
coupling (gridded HRRR → wind binaries) — a major rebuild, deferred.

**Resolution / "full ForeFire fidelity" — tested, minor.** The engine is coarsened
for the ~150 s web budget (`perim_res` up to 2.5 km on big fires, 100×100 layers,
~30×30 fuel). All of that is now configurable (`FOREFIRE_GRID_N`,
`FOREFIRE_PERIM_RES_DIV/MIN/MAX`, `FOREFIRE_TIME_BUDGET_S`, `FUEL_GRID_SAMPLES`,
`ELEV_GRID_N`), so validation can run near ForeFire's real resolution. At high
fidelity (grid 200, front ~fire/200 floor 80 m, 60×60 fuel) the gain is small and
inconsistent — Carr +0.03 skill, Wallow flat, Thomas −0.02 — for several× the
runtime. So the coarsening is a *real but minor* limitation; it's an opt-in
high-fidelity mode, not a default.

**Fire set.** ~35 candidate fire-days across regions/years/sizes (megafire run days,
moderate days, and smaller normal fires); ~16 pass the fairness filter on any run.
Date-guess misses print the available GeoMAC dates so they can be corrected.

**Suppression damping (`suppression_scaling`, default on) — done.** A free-spread
model over-predicts fires crews/lines are holding. We damp the driving wind by a
suppression signal in [0,1]: reported containment (production) or recent growth
momentum (validation — a fire that barely grew the prior day is likely being held).
On the 16-fire set it re-centred bias 1.03 → 1.00 and improved the slowing fires
(Carr 7/28 1.56→1.33, Rim 8/24 1.40→1.28). It can't catch a fire that exploded and
was then abruptly contained (momentum still reads "active") — that needs real
per-day containment, which GeoMAC doesn't carry.

**Coupled atmospheric wind — investigated to a definitive dead end.** Canonical
ForeFire (Corsica, FireCaster) couples to Meso-NH/WRF for spatiotemporal, plume-fed
wind. That is NOT reachable from the standalone `pyforefire` API. Proven by reading
the source + three engine tests: (a) a spatial wind field IS sampled per-node
(`getNormalWind`→`getValueAt`), but (b) plain data layers are static — the field is
copied at creation, runtime updates are ignored — and (c) the per-step `trigger`
that gives temporal evolution overwrites any spatial field globally. The only
spatiotemporal path, `loadMultiWindBin`, lives inside ForeFire's Meso-NH coupling
mode (`runmode="masterMNH"`) — it requires running ForeFire *inside* a mesoscale
atmospheric model writing gridded wind binaries. A keyless web service can supply
neither the model nor gridded HRRR. **Gust blending already captures the main
achievable piece** (the wind *magnitude* the mean misses); genuine spatial/plume
coupling would be a different architecture, not an increment.

**Model progression** (fair real-perimeter set): raw free-spread under-predicts
(bias ~0.6) → gusts+spotting fix run days (0.99) but over-predict normal ones →
regime scaling balances both → suppression re-centres the mixed set at bias 1.00.

### ML residual correction (Phase 5) — trained, validated, and ON by default

The physics corrections above are hand-tuned. On top of them, a learned model
predicts the *remaining* residual (observed ÷ forecast area) from forecast-time
features and rescales the footprint. Pipeline (all in `validation/`):

- **`harvest.py`** — enumerate GeoMAC (2000–2019) into fire-day pairs (6,526 from
  1,688 fires; `batch_harvested.json`).
- **`build_features.py`** — run each through the model, record features + residual +
  footprint geometry (`features.csv`, `features_geom.jsonl`). Resumable.
- **`build_model.py`** — a gradient-boosted tree; by-fire CV R² on log-residual
  climbs with data (0.04 @135 → 0.20 @400 → **0.28 @691 rows**). No single feature
  predicts the residual (slope is the strongest at ρ≈−0.38); the model wins by
  *combining* weak signals — which is exactly why hand rules (regime-by-HDW,
  suppression-by-momentum) matched only a flat global recalibration.
- **`phase4_skill.py`** — applies the correction geometrically under by-fire CV and
  re-scores Jaccard. **+0.05 Jaccard held-out overall, +0.10 on quiet days, ~0 on
  run days (no harm).**
- **`train_correction.py`** — fits the shipped model → `app/ml/correction_model.joblib`.
- **`parity_check.py`** — confirms the features the backend computes at inference
  match the training features (13/14 tight; only `log_t0` off ~4% from a projection
  origin). So the Phase-4 number is the *production* number.

Wired into the adapter behind `ml_correction` (**default on**; no-op if the model /
scikit-learn are absent). Pass `momentum` (recent growth ratio) in the request for
the full benefit — it's the #2 feature; new fires without history just get NaN,
which the model tolerates. `scikit-learn` is version-pinned so the model unpickles
under the same version it was trained with. Residual caveat: live forecasts use
HRRR wind over the forecast horizon, training used ERA5 over a 2-day window — a small
skew in the (low-importance) wind features only.

### Read the RUN-DAY skill, not the mixed mean

The batch summary now splits fair fires into **run days** (grew ≥100%) and
**moderate days** (<100%), because persistence ("no change") is near-unbeatable on
days a fire barely moves — so the mixed mean understates the model by construction:

```
RUN DAYS (grew ≥100%, 6)      Jacc 0.34  skill +0.112  bias 0.54   beats persistence 6/6
MODERATE DAYS (grew <100%,10) Jacc 0.57  skill −0.078  bias 1.28   beats persistence 4/10
ALL FAIR (16)                 Jacc 0.48  skill −0.007  bias 1.00   beats persistence 10/16
```

On the days that matter for evacuation the model beats persistence **6/6** with
**+0.11 skill**; it treads water on quiet days (where nothing beats "no change").
A near-zero *mixed* skill is not "no better than a coin flip" — the baseline is
persistence, a strong one, and the set is deliberately half persistence-dominated.
(Run days still under-predict the very largest runs — bias 0.54 — the residual the
surface model structurally can't reach.)

### Time-varying wind — already used; validation is wind-fair

The model already ingests **hourly, time-varying** wind (production: Open-Meteo's
HRRR-backed forecast; hindcast: ERA5 archive) and re-triggers it each step so the
fire bends as the wind shifts. The only wind gaps are *spatial* (blocked — see
above) and ERA5's coarse *magnitude*, which gust-blending fixes. Measured at a
recent fire location, ERA5+gust effective wind (36 km/h) actually exceeds HRRR+gust
(29) — so the ERA5-based hindcast is **not** wind-starved vs production; the skill
numbers are fair. An `hrrr` wind source (`--wind-source hrrr`, `historical-forecast-api`,
~3 km, 2021+, the model production uses) is wired in for recent-fire validation once
recent daily perimeters are available (GeoMAC's clean daily archive ends 2019; WFIGS
doesn't retain historical daily snapshots, so that's the current blocker).

---

# Prospective forecast validation

Does the ForeFire forecast actually predict where a fire goes? You can't know until
the fire moves — so this harness **snapshots a forecast for an active fire now**,
and a day or two later **scores the predicted footprint against the fire's real,
re-mapped perimeter**. It uses the live pipeline unchanged (no historical data).

## Prerequisites

- The backend running with ForeFire — i.e. the **Docker image on `:8000`**
  (`/predict` needs `pyforefire`). See the repo README.
- Run the script from the backend's Python env (needs `httpx` + `shapely`, both in
  `requirements.txt`):

  ```bash
  cd backend
  ./.venv/Scripts/python validation/prospective_validation.py <command>   # Windows
  # or:  python validation/prospective_validation.py <command>
  ```
  Point at a different backend with `WILDFIRE_API=http://host:8000`.

## Workflow

```bash
# 1. Find good candidates: large, actively spreading (low % contained), has a perimeter
python validation/prospective_validation.py candidates --min-acres 2000 --max-contained 40

# 2. Snapshot a forecast NOW (saves the 24h forecast + the current perimeter as T0)
python validation/prospective_validation.py snapshot --lat 37.734 --lon -109.809 --hours 24

# 3. ~24–48 h later (after the perimeter is re-mapped), score it
python validation/prospective_validation.py score --file validation/snapshots/<file>.json
```

Snapshots are saved under `validation/snapshots/` (git-ignored). Scoring also writes
a `*_overlay.geojson` with three layers — **T0 (start)**, **forecast**, **observed** —
that you can drag onto https://geojson.io or open in QGIS to see the overlap.

## Reading the score

For the forecast footprint vs the observed later perimeter:

| Metric | Meaning | Perfect |
|---|---|---|
| **Jaccard** | intersection ÷ union of the burned areas | 1.0 |
| **Dice** | 2·intersection ÷ (pred + obs area) | 1.0 |
| **area bias** | predicted area ÷ observed area | 1.0 (>1 over-predicts) |

The key column is the **persistence baseline**: the same metrics for the *T0*
perimeter vs the observed one — i.e. "what if you assumed the fire didn't move?"
The forecast only demonstrates skill if **Forecast Jaccard − baseline > 0**. Both
footprints share the T0 area, so raw overlap is high for any slow fire; the
baseline is what strips that away.

## Interpreting honestly (important)

This is a **free-spread surface model**. Real fires are shaped by things it doesn't
model, so read scores with that in mind:

- **Suppression** — crews, dozer lines, and retardant stop fires. A forecast that
  assumes free spread will **systematically over-predict** (area bias > 1). This is
  the single biggest reason a *correct* model disagrees with reality. **Prefer
  low-containment fires** so suppression confounds the least.
- **Wait for a re-map.** NIFC perimeters update ~daily and lag. If `score` says
  *"observed perimeter essentially unchanged from T0,"* it hasn't been re-mapped
  yet — score again later. Scoring too early is meaningless (the forecast predicts
  growth the perimeter doesn't show yet).
- **Spotting, crown fire, fire–atmosphere feedback**, plus our own approximations
  (~2–3 km fuel grid, one wind vector, point-derived slope, fixed live moisture) all
  add error.

So a modest Jaccard doesn't necessarily mean the *integration* is wrong — it may be
the model's inherent ceiling or an actively-suppressed fire. Use several fires,
prefer uncontained ones, and watch the **skill-over-baseline** and **area-bias**
trends rather than any single number. To isolate whether the *wiring* is right
(rather than model skill), see the physical-response and single-point Rothermel
cross-checks described when we set this up.
