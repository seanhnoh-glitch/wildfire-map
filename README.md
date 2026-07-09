# Wildfire Map 🔥🗺️

An interactive map of **active US wildfires**. Open it, see every ongoing fire and
its mapped perimeter, tap one, and get a **forecast of where it's predicted to
spread** over the next 24 hours — driven by a real fire-behavior simulator using
live wind, fuel, terrain, and moisture.

- **Backend:** FastAPI service that aggregates live fire, weather, fuel, and
  terrain data and runs the spread simulation. It also **serves the web map**.
- **Web map (primary UI):** a single-page MapLibre map served at `/` — works in
  any browser, desktop or phone (including iPhone Safari). No build step.
- **Prediction:** **[ForeFire](https://github.com/forefireAPI/forefire)** — a C++
  front-tracking fire simulator (Rothermel/Farsite surface spread), run via its
  `pyforefire` bindings. It is the only prediction engine.
- **Mobile app** (`mobile/`): an earlier React Native (Expo) client. **Secondary
  and not actively maintained** — the web map is the primary UI. See the note
  below.

> ⚠️ Research/education project. Forecasts are **not** operational fire-behavior
> guidance. In a real emergency follow official sources (InciWeb, Watch Duty,
> local authorities).

## What it does

| Feature | Status |
|---|---|
| Every active US wildfire nationwide (NIFC WFIGS points) | ✅ live |
| Mapped fire perimeters, detail-on-demand when you zoom in | ✅ live |
| Satellite hotspots (NASA FIRMS, VIIRS/NOAA-20) when zoomed in | ✅ live *(needs a free key)* |
| Live weather / wind (NWS → Open-Meteo) | ✅ live |
| Fuel across the fire domain (LANDFIRE FBFM40 grid) | ✅ live |
| **Water / urban / rock as non-burnable barriers the fire stops at** | ✅ live |
| Dead-fuel moisture from live humidity/temperature (Simard EMC) | ✅ live |
| 10 m → midflame wind reduction (per-fuel adjustment factor) | ✅ live |
| Real terrain slope **and aspect** (uphill direction) | ✅ live |
| HRRR-backed hourly forecast wind → fire bends as the wind shifts | ✅ live |
| Ignition from the real NIFC perimeter footprint | ✅ live |
| ForeFire front-tracking simulation → animated 24 h isochrones | ✅ live |
| **Traffic-aware evacuation routes away from the fire** (Mapbox + FEMA/OSM shelters) | ✅ live *(needs a free Mapbox token for drive routes)* |

Full source list: **[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)**. How the ForeFire
engine is wired: **[docs/FOREFIRE_SETUP.md](docs/FOREFIRE_SETUP.md)**.

## Quickstart (Docker — recommended)

ForeFire's `pyforefire` bindings are compiled from C++ against NetCDF and are
**not** on PyPI, so the backend runs in Docker (it builds ForeFire for you).

```bash
cd backend
cp .env.example .env          # then add your free FIRMS_MAP_KEY (optional; for hotspots)
docker build -t wildfire-map-backend .
docker run -p 8000:8000 --env-file .env wildfire-map-backend
```

Open **http://localhost:8000** — that's the map. Interactive API docs at
**http://localhost:8000/docs**.

Get a free FIRMS key in ~1 min: https://firms.modaps.eosdis.nasa.gov/api/map_key/

### Running locally without Docker (no forecasts)

You can run the API with plain Python, but **`/predict` will return HTTP 503**
because `pyforefire` isn't installed — everything else (fires, perimeters,
weather, geocoding, hotspots) works:

```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Run the offline unit tests with `python -m pytest`.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The web map (single-page app) |
| GET | `/geocode?address=` | Address / place → lat/lon |
| GET | `/fires/all?min_acres=&limit=` | Every active US wildfire (points) |
| GET | `/perimeters/all?min_acres=` | All mapped perimeters (simplified) |
| GET | `/perimeters/bbox?west=&south=&east=&north=` | Full-res perimeters in a viewport |
| GET | `/hotspots/bbox?west=&south=&east=&north=` | FIRMS hotspots in a viewport |
| GET | `/fires/nearby?lat=&lon=&radius_km=` | Fires + perimeters + hotspots near a point |
| GET | `/weather?lat=&lon=` | Current wind/temp/RH at a point |
| POST | `/predict` | Spread forecast → GeoJSON isochrones (ForeFire) |
| POST | `/evacuation` | Traffic-aware routes away from a fire to a safe destination |
| GET | `/health` | Status + propagation model |

Example:

```bash
curl -X POST http://localhost:8000/predict -H "Content-Type: application/json" \
     -d '{"lat":39.5,"lon":-121.6,"duration_hours":24,"step_minutes":60}'
```

## Architecture

```
web map  backend/app/web/index.html   (served at /)   ← primary UI
   │  REST / JSON
   ▼
backend (FastAPI)
   ├─ routers/            thin HTTP layer
   └─ services/
        geocoding.py      US Census → OpenStreetMap Nominatim
        fires.py          NIFC WFIGS points/perimeters + NASA FIRMS hotspots
        weather.py        NWS → Open-Meteo (current + HRRR-backed hourly)
        fuel.py           LANDFIRE FBFM40 (point + domain grid) → fuel codes
        fuel_table.py     FARSITE fuel table + non-burnable barrier row
        terrain.py        Open-Meteo elevation → slope + aspect
        spread_model.py   perimeter → shapely polygon (ignition footprint)
        forefire_adapter.py  gathers inputs, runs ForeFire, returns isochrones
        evacuation.py     safe destinations (FEMA/OSM) + Mapbox traffic routing
```

The ForeFire simulation runs in a **fresh spawned subprocess** per request (the
engine keeps process-global C++ state, so each forecast needs a clean process).

## The mobile app (secondary)

`mobile/` is a React Native (Expo + MapLibre) client from an earlier phase. It
still works against the API and benefits from all the backend modeling
improvements, but it does **not** have the web map's newer UI features (viewport
hotspots, dots centered on perimeters, the 24 h horizon and color styling). The
**web map served at `/` is the maintained UI.** Setup, if you want it anyway:
**[docs/MOBILE_SETUP.md](docs/MOBILE_SETUP.md)**.

## Roadmap ideas

- Full-resolution (30 m) water masking via a LANDFIRE raster export (today's
  barrier grid is ~2–3 km, so it catches large water but not small ponds).
- Raw NOMADS HRRR GRIB grids for a *spatial* wind field (not one point).
- Fuel moisture that also accounts for recent precipitation and diurnal lag
  (Nelson dead-fuel model), and live fuel moisture from greenness/season.
- Bring the mobile app to parity, or retire it in favor of the web map.
