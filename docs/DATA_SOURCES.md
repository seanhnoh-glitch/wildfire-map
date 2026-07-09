# Data Sources & APIs

Every external source this project uses, what it provides, whether it needs a key,
and how it feeds the model. All are US-focused, free, and public. This is the
reference list for the four model ingredients — **fire location, fuel, terrain,
weather** — plus geocoding and map tiles.

Legend: 🔑 needs a (free) key · 🆓 no key · ⏳ wired but not yet fully implemented

---

## 1. Where the fire is (ignition + display)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **NIFC WFIGS — Incident Locations (Current)** 🆓 | Authoritative current US wildfire *points*: name, size (acres), % contained, discovery time, county/state. New fires appear here fastest. | none | `services/fires.py` → `/fires/all`, `/fires/nearby` |
| **NIFC WFIGS — Interagency Perimeters (Current)** 🆓 | Mapped fire *footprints* (polygons). Lags 12–24 h behind the point feed. | none | `services/fires.py` (perimeters + ignition seed) |
| **NASA FIRMS** 🔑 | Near-real-time satellite thermal *hotspots* (VIIRS aboard NOAA-20), refreshed every few hours. Raw detections, includes fires too new to be in NIFC. | free MAP_KEY | `services/fires.py` (`FIRMS_MAP_KEY`) → `/hotspots/bbox` |

- FIRMS API + free key: https://firms.modaps.eosdis.nasa.gov/api/map_key/
- We query `VIIRS_NOAA20_NRT` over a **2-day** window (NRT for the current day
  lags a few hours, so a 1-day window often misses "yesterday's" detections).
- **How it feeds the model:** the incident point locates the fire; its NIFC
  *perimeter* is the ignition footprint the simulation grows from.

## 2. Fuel — what's burning (most important model input)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **LANDFIRE 2022 FBFM40** 🆓 | 30 m US raster of Scott & Burgan (2005) **40 fire behavior fuel models** (grass/shrub/timber/slash) plus non-burnable classes (water, urban, rock). The definitive US fuel dataset. | none | `services/fuel.py` |

- ImageServer (CONUS): `lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer`
- **Point lookup** (`fuel_at`, `identify` op) → the fuel code at the fire, used to
  pick the default fuel and its wind adjustment factor.
- **Domain grid** (`fuel_grid`, `getSamples` op) → a coarse (~30×30, 2–3 km cells)
  grid of fuel codes across the whole fire domain. Burnable codes (101–204) pass
  straight into ForeFire's FARSITE fuel table; **water/urban/rock/no-data become a
  non-burnable "barrier"** (fuel index 999 in `fuel_table.py`) so the fire stops
  at them instead of crossing them.
- **How it feeds the model:** ForeFire keys its Rothermel/Farsite rate-of-spread
  off the fuel code at each cell; barrier cells get a rate of spread of zero.

## 3. Terrain — slope drives uphill spread

| Source | Provides | Key | Used in |
|---|---|---|---|
| **Open-Meteo Elevation** 🆓 | Point elevation (Copernicus DEM). We sample a small N/S/E/W cross and take the central-difference gradient → **slope magnitude and uphill aspect (bearing)**. | none | `services/terrain.py` (`slope_aspect_at`) |
| **USGS 3DEP** ⏳ | 1–10 m US elevation (DEM). A full pipeline would clip a 3DEP tile as a proper elevation grid instead of a point estimate. | none | (future) |

- **How it feeds the model:** ForeFire's slope layer is a tilted plane whose
  gradient magnitude equals the local slope and whose uphill direction is the real
  aspect, so fire runs uphill in the correct direction.

## 4. Weather — wind is the #1 dynamic driver

| Source | Provides | Key | Used in |
|---|---|---|---|
| **NWS api.weather.gov** 🆓 | Official US current conditions: wind, temp, RH. | none | `services/weather.py` (`current`, primary) |
| **Open-Meteo Forecast** 🆓 | Global current + **hourly forecast** wind/temp/RH. `current` fallback AND the source of hourly forecast wind. | none | `services/weather.py` (`current` fallback, `forecast_hourly`) |
| **NOAA HRRR (via Open-Meteo)** ✅ | 3 km hourly wind *forecast*. Open-Meteo's `best_match` uses HRRR for short-range US, so `forecast_hourly` is HRRR-quality without GRIB parsing. | none | `services/weather.py` (`forecast_hourly`) |
| **NOAA HRRR raw grids (NOMADS)** ⏳ | Direct GRIB2 for a *spatial* wind field (not one point). | none | (future) |

- **How it feeds the model:**
  - **Wind** sets rate of spread and which way the fire is pushed. Weather reports
    the direction wind blows *from*; the model pushes the fire the opposite way.
    The 10 m forecast wind is reduced to **midflame** wind by a per-fuel adjustment
    factor before it drives the simulation, then re-triggered each hourly step so
    the fire **bends as the wind shifts**.
  - **Temperature + humidity** drive **dead fuel moisture** via the Simard (1968)
    equilibrium-moisture-content model (drier air → drier fuel → faster spread).

## 5. Address → coordinates (geocoding)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **US Census Geocoder** 🆓 | US street address → lat/lon. Best for full addresses; no city-only matches. | none | `services/geocoding.py` (primary) |
| **OpenStreetMap Nominatim** 🆓 | Free-form search (cities, towns, landmarks). Fallback so bare place names resolve. | none | `services/geocoding.py` (fallback) |

- Census: https://geocoding.geo.census.gov/geocoder/ · Nominatim: https://nominatim.openstreetmap.org/

## 6. Map tiles (the base map)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **OpenStreetMap raster** 🆓 | Keyless base map for development. **Not** for production traffic (usage policy). | none | web `app/web/index.html`; `mobile/src/lib/mapStyle.ts` |
| **MapTiler / Stadia / self-hosted** ⏳ | Production vector/raster tiles, satellite imagery. | key | (swap the `tiles` URL) |

- Rendering: **MapLibre GL** in the web map; `@maplibre/maplibre-react-native` in
  the mobile app. Free, no per-tile billing, no token.

## 7. Evacuation routing (route away from the fire)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **Mapbox Directions — `driving-traffic`** 🔑 | Turn-by-turn driving routes **with live traffic** + alternatives + typical-vs-current ETA. Each candidate is filtered so any route crossing the fire/forecast polygon is dropped. | free token | `services/evacuation.py` (`MAPBOX_TOKEN`) → `/evacuation` |
| **FEMA National Shelter System — Open Shelters** 🆓 | Shelters that are **actually open now**, synced from the **American Red Cross** database (refreshed daily + every 20 min). The authoritative "go here" list — sparse on calm days, populated during real evacuations. [gis.fema.gov NSS/OpenShelters](https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/FeatureServer/0) | none | `services/evacuation.py` |
| **OpenStreetMap Overpass** 🆓 | Real named fallbacks near, but clear of, the fire: `emergency=assembly_point`, hospitals, community centres, and **towns/cities/villages**. (OSM `amenity=shelter` is skipped — it's mostly picnic/transit shelters.) | none | `services/evacuation.py` |
| **Mapbox reverse geocoding** 🔑 | Snaps the geometric fallback points to the **nearest real town** so even the last-resort target has a name. | Mapbox token | `services/evacuation.py` |
| **Computed safe points** 🆓 | Geometric last resort — points a safe distance away in the "away from fire" directions (then town-snapped), so a route target always exists. | none | `services/evacuation.py` |

- Mapbox token (free ~100k req/mo): https://account.mapbox.com/access-tokens/
- Destinations are a **hybrid**, best-first: currently-open FEMA/Red Cross shelters →
  OSM assembly points / hospitals / community centres / towns → geometric points
  snapped to the nearest town. Without a Mapbox token, `/evacuation` still returns
  the safe destinations (just no drive routes or town-snapping).

---

## The prediction pipeline (how the pieces combine)

```
NIFC perimeter (or incident point)
        │  ignition footprint
        ▼
gather inputs ──► wind      (NWS → Open-Meteo, HRRR-backed hourly)
        │         moisture  (temp + RH → Simard EMC)
        │         fuel grid (LANDFIRE FBFM40 across the domain, +barriers)
        │         slope+aspect (Open-Meteo elevation gradient)
        ▼
ForeFire (spawned subprocess): FARSITE surface spread on the fuel/wind/
        │  slope layers, seeded from the perimeter, stepped hour by hour
        ▼
GeoJSON isochrones ──► web map animates the 24 h forecast
```

## The ForeFire model — briefly

`/predict` runs **[ForeFire](https://github.com/forefireAPI/forefire)**, a C++
front-tracking simulator, via its `pyforefire` bindings (see
[FOREFIRE_SETUP.md](FOREFIRE_SETUP.md)). Per request, in a clean subprocess,
`services/forefire_adapter.py`:

- Sizes a local-metre domain around the fire and lays down four layers:
  - **fuel** — the LANDFIRE FBFM40 grid, with water/urban/rock as non-burnable
    barriers, against ForeFire's FARSITE fuel table (`fuel_table.py`);
  - **wind** — the midflame-reduced forecast wind, re-triggered each step;
  - **slope** — a plane tilted along the real terrain aspect;
  - **moisture** — dead fuel moisture from live temperature/humidity.
- **Ignites** a real `FireFront` traced from the NIFC perimeter (simplified to the
  working resolution so the shape is preserved), or a small front for a point
  ignition when no perimeter is nearby.
- **Steps** the model hour by hour with the `Farsite` (Rothermel-family)
  propagation model, exporting each step's front as a GeoJSON `Polygon` tagged with
  `hours`, `head_distance_km`, and `area_km2` — nested isochrones the map animates.

There is **no fallback engine**: if `pyforefire` is unavailable, `/predict` returns
HTTP 503.

**References:** Rothermel (1972); Andrews (2012, wind adjustment factor);
Simard (1968, EMC); Scott & Burgan (2005, fuel models); Filippi et al. (ForeFire).
This is a research/education tool — **not** operational fire-behavior guidance.
