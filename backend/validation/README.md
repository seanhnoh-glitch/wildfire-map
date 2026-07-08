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
