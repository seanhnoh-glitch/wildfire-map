# Wiring the real ForeFire engine

The app runs today on the **built-in elliptical model**. This guide covers
upgrading the prediction engine to **ForeFire** (the C++ front-tracking fire
simulator). When ForeFire is available and `PREDICTION_ENGINE=auto|forefire`,
the backend routes `/predict` through it automatically via
`services/forefire_adapter.py`; nothing in the mobile app changes.

## What ForeFire is

- Repo: https://github.com/forefireAPI/forefire
- A C++ simulation engine (front-tracking / level-set with Rothermel-family
  rate-of-spread). Driven either by:
  - **Python bindings** `pyforefire` (built from source), or
  - a compiled **`forefire`** binary fed a command script.
- It is **not** a hosted web API — you run it on your own server.

## Step 1 — Build ForeFire

```bash
git clone https://github.com/forefireAPI/forefire.git
cd forefire
# Needs a C++ compiler, CMake, and NetCDF. On Debian/Ubuntu:
sudo apt-get install cmake libnetcdf-dev g++
cmake -B build && cmake --build build
# Binary: ./build/forefire   (set FOREFIRE_BINARY to this path)
# Optional Python bindings:
pip install ./py   # or follow the repo's pyforefire build instructions
```

Then in `backend/.env`:

```
PREDICTION_ENGINE=auto
FOREFIRE_BINARY=/abs/path/to/forefire/build/forefire
```

## Step 2 — Build the landscape (the real integration work)

ForeFire needs a **NetCDF landscape** with fuel, elevation, and wind on a common
grid, plus a `fuels.ff` parameter file. Implement this inside
`forefire_adapter._run_forefire`:

1. **Clip fuel** — pull a LANDFIRE FBFM40 raster window around the fire
   (`services/fuel.py` is the seam; extend it to fetch a raster, not just a point).
2. **Clip elevation** — pull a USGS 3DEP DEM window for the same extent
   (`services/terrain.py` seam).
3. **Wind** — from `services/weather.py` (a single vector to start; later a
   spatial HRRR field for a time-varying forecast).
4. **Write NetCDF** — resample fuel + elevation to one grid and write the
   ForeFire landscape file (see ForeFire's `tools/` and example landscapes).
5. **Map fuels** — produce a `fuels.ff` mapping each LANDFIRE fuel code to
   ForeFire fuel parameters (the fuel crosswalk — the biggest science task).

## Step 3 — Run and export

- **Ignition:** set an ignition point, or import the NIFC perimeter polygon as
  the starting front (`ignite_from_perimeter`).
- **Simulate:** step the model to `duration_hours`.
- **Export:** convert ForeFire fronts (isochrones) to a GeoJSON
  `FeatureCollection` matching the built-in model's output shape
  (`Polygon` features with `hours` / `head_distance_km` / `area_km2` props) so the
  mobile app renders them unchanged.

## Recommended order

Get the built-in model demoing end-to-end first (it already works). Then wire
ForeFire on a **single prototype region** — the fuel crosswalk + NetCDF assembly
is where projects stall, so validate it small before scaling to all of the US.
```
