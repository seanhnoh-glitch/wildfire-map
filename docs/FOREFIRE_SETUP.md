# ForeFire engine

The `/predict` forecast is produced by **[ForeFire](https://github.com/forefireAPI/forefire)**,
a C++ front-tracking fire simulator (Rothermel/Farsite surface spread), driven via
its `pyforefire` Python bindings. This is **the** prediction engine — there is no
fallback. This doc explains how it's built and how the integration works.

## What ForeFire is

- A C++ simulation engine (front-tracking / level-set with a Rothermel-family
  rate-of-spread). Not a hosted API — you run it yourself.
- We drive it through the **`pyforefire`** bindings, which are compiled from the
  ForeFire C++ source (against NetCDF). They are **not** on PyPI, which is why the
  backend ships as a Docker image that builds them.

## Build & run (Docker)

`backend/Dockerfile` does the whole build — clone ForeFire, compile it with CMake,
install `pyforefire`, then install the app:

```bash
cd backend
docker build -t wildfire-map-backend .
docker run -p 8000:8000 --env-file .env wildfire-map-backend
```

That's it — `/predict` uses ForeFire automatically. `/health` reports the active
propagation model. If `pyforefire` can't be imported, `/predict` returns HTTP 503.

## How the integration works

All of it lives in `backend/app/services/forefire_adapter.py`. Because ForeFire
keeps process-global C++ state, **each forecast runs in a fresh spawned
subprocess** (`ProcessPoolExecutor` with the `spawn` context) — otherwise the
second fire would inherit the first fire's domain.

`_gather_inputs()` (async, main process) collects the live inputs:

1. **Wind** — HRRR-backed hourly forecast from `weather.py`, one vector per step.
2. **Fuel grid** — `fuel.fuel_grid()` samples the LANDFIRE FBFM40 raster across the
   domain (`getSamples`); burnable codes pass through, water/urban/rock/no-data
   become the non-burnable **barrier** index (999).
3. **Moisture** — dead fuel moisture from live temp/RH via the Simard EMC model.
4. **Slope + aspect** — `terrain.slope_aspect_at()` (elevation gradient).
5. **Ignition footprint** — the NIFC perimeter nearest the point (`fires.py`),
   converted to a shapely polygon (`spread_model.perimeter_to_polygon`).

`_run_forefire()` (in the subprocess) then:

1. Sizes a local-metre `FireDomain` around the fire (contains the perimeter + room
   to spread; front resolution scales with fire size to bound runtime).
2. Sets the propagation model (`Farsite`), the fuel table
   (`fuel_table.FARSITE_FUEL_TABLE` = ForeFire's standard FARSITE table + the
   barrier row), and the moisture parameters.
3. Adds layers: **fuel** (the LANDFIRE grid), **wind** (reduced from 10 m to
   midflame by a per-fuel adjustment factor), and **altitude** (a plane tilted
   along the real aspect).
4. Seeds a `FireFront` traced from the perimeter (Douglas–Peucker–simplified to the
   working resolution so the shape is preserved), or a small front for a point
   ignition.
5. Steps hour by hour, re-triggering the wind each step, and parses each front from
   `print[]` into a GeoJSON `Polygon` (`hours` / `head_distance_km` / `area_km2`),
   returning nested isochrones. A wall-clock budget returns a partial forecast
   rather than hanging on a pathologically large fire.

## The fuel table & the non-burnable barrier

ForeFire's built-in `STDfarsiteFuelsTable` is keyed by the LANDFIRE FBFM40 indices
(101–204), so real fuels map 1:1. But the raster's non-burnable codes (91–99) have
a fuel-bed depth of 0, and the Farsite rate-of-spread calc divides by depth →
**NaN**, which corrupts the front. So `fuel_table.py` appends one custom row
(index 999) with a tiny nonzero load/depth (no divide-by-zero) and a moisture of
extinction below any real fuel moisture, which forces the rate of spread cleanly to
**0**. Water and other non-burnable cells map to 999 and the fire simply stops.

## Known limitations / next steps

- **Fuel-grid resolution** is ~2–3 km (the `getSamples` cap is ~1,000 points), so
  large water bodies act as barriers but small ponds/creeks don't. Full 30 m
  masking would need a raster (GeoTIFF/LERC) export instead.
- **Elevation** is a uniform slope plane from a point estimate, not a real DEM
  window; a 3DEP clip would give true terrain.
- **Live fuel moisture** and the wind reduction factor are single representative
  values, not spatially varying.
- **Wind** is one vector over the domain per step, not a spatial HRRR field.
