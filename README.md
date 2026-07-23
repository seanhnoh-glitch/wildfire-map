# Wildfire Map 🔥🗺️

An interactive map of **active US and Canadian wildfires**. Open it, see every
ongoing fire and its mapped perimeter, tap one, and get a **forecast of where it's
predicted to spread** over the next 24 hours — driven by a real fire-behavior
simulator using live wind, fuel, terrain, and moisture.

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
> guidance. In a real emergency follow official sources — **US:** InciWeb, Watch
> Duty, local authorities; **Canada:** CWFIS, your provincial/territorial emergency
> management, and the Canadian Red Cross.

## What it does

| Feature | Status |
|---|---|
| Every active **US + Canadian** wildfire nationwide (NIFC WFIGS + CWFIS points) | ✅ live |
| Mapped perimeters — **US NIFC** surveyed lines + **Canadian CWFIS M3** satellite footprints, detail-on-demand when you zoom in | ✅ live |
| **One dot per mapped footprint** (perimeter-driven), sized to the drawn fire, dropped onto the polygon | ✅ live |
| Satellite hotspots (NASA FIRMS, VIIRS/NOAA-20) when zoomed in | ✅ live *(needs a free key)* |
| Live weather / wind (NWS → Open-Meteo) | ✅ live |
| Fuel across the fire domain — **LANDFIRE FBFM40 (US CONUS + Alaska)** and **CWFIS/CFFDRS FBP (Canada)** | ✅ live |
| **Water / urban / rock as non-burnable barriers the fire stops at** | ✅ live |
| Dead-fuel moisture from live humidity/temperature (Simard EMC) | ✅ live |
| 10 m → midflame wind reduction (per-fuel adjustment factor) | ✅ live |
| Real terrain slope **and aspect** (uphill direction) | ✅ live |
| HRRR-backed hourly forecast wind → fire bends as the wind shifts; **wind veer/backing shown** | ✅ live |
| Ignition from the real mapped perimeter footprint | ✅ live |
| ForeFire front-tracking simulation → animated 24 h isochrones | ✅ live |
| **Containment credit** — a partly-contained fire only grows along its *uncontained* perimeter | ✅ live |
| **No forecast for fully-controlled fires** — US 100% contained / Canada "Under Control" | ✅ live |
| **Directional-spread diagnostic** — head vs backing growth, so you can tell a wind-driven run from uniform spread | ✅ live |
| **Traffic-aware evacuation routes away from the fire**, **country-aware** (FEMA/OSM in the US, Canadian Red Cross/OSM in Canada) with reverse-geocoded **street addresses** | ✅ live *(Mapbox token upgrades drive routes to live traffic; keyless OSRM otherwise)* |

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
| GET | `/geocode?address=` | Address / place (US or Canada) → lat/lon |
| GET | `/fires/all?min_acres=&limit=` | Every active US + Canadian wildfire (points) |
| GET | `/perimeters/all?min_acres=` | All mapped perimeters, US + Canada (simplified) |
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
        geocoding.py      US Census → OpenStreetMap Nominatim (US + Canada); reverse
        fires.py          NIFC WFIGS + CWFIS points/perimeters + NASA FIRMS hotspots
        weather.py        NWS → Open-Meteo (current + HRRR-backed hourly)
        fuel.py           LANDFIRE FBFM40 (US CONUS + Alaska) & CWFIS/CFFDRS FBP (Canada)
        fuel_table.py     FARSITE fuel table + non-burnable barrier row
        terrain.py        Open-Meteo elevation → slope + aspect
        spread_model.py   perimeter → shapely polygon (ignition footprint)
        forefire_adapter.py  gathers inputs, runs ForeFire, containment credit, isochrones
        evacuation.py     country-aware safe destinations (FEMA/Red Cross/OSM) + routing
```

Fuel source is chosen per fire: **US CONUS or Alaska → LANDFIRE**, **Canada → CWFIS
FBP** (both encode water/urban/rock as the non-burnable barrier the fire stops at).
Map dots are **perimeter-driven** — one dot per drawn footprint, US fires matched to
their perimeter by name, Canadian fires (whose CWFIS points and M3 satellite polygons
are separate datasets) matched spatially.

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

- Higher-resolution water masking (today's barrier grid is ~2–3 km, so it catches
  large water but not small ponds).
- Fuel coverage for the **Alaska panhandle / far Aleutians** (west of −170°) and any
  other gaps outside the LANDFIRE-CONUS/Alaska + CWFIS footprints (they fall back to a
  labelled regional-default fuel today).
- Field-validate the **Canadian FBP → FBFM40 crosswalk** (boreal conifer currently maps
  to TL3 surface litter, with crown behaviour from the spotting model).
- Raw NOMADS HRRR GRIB grids for a *spatial* wind field (not one point).
- Fuel moisture that also accounts for recent precipitation and diurnal lag
  (Nelson dead-fuel model), and live fuel moisture from greenness/season.
- Bring the mobile app to parity (it still renders US-style only), or retire it in
  favor of the web map.
