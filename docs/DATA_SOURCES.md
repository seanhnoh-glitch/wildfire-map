# Data Sources & APIs

Every external source this project uses, what it provides, whether it needs a key,
and how it feeds the model. All are free and public, covering **the US and Canada**.
This is the reference list for the four model ingredients — **fire location, fuel,
terrain, weather** — plus geocoding, evacuation, and map tiles.

Legend: 🔑 needs a (free) key · 🆓 no key · ⏳ wired but not yet fully implemented

---

## 1. Where the fire is (ignition + display)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **NIFC WFIGS — Incident Locations (Current)** 🆓 US | Authoritative current US wildfire *points*: name, size (acres), % contained, discovery time, county/state. New fires appear here fastest. | none | `services/fires.py` → `/fires/all`, `/fires/nearby` |
| **NIFC WFIGS — Interagency Perimeters (Current)** 🆓 US | Mapped fire *footprints* (polygons). Lags 12–24 h behind the point feed. | none | `services/fires.py` (perimeters + ignition seed) |
| **CWFIS — Active Wildfires in Canada** 🆓 CA | Current Canadian wildfire *points*: name, size (hectares → acres), **stage of control** (Out of Control / Being Held / Under Control — Canada reports this instead of a %), start date, agency/province. | none | `services/fires.py` → `all_active_ca` |
| **CWFIS — Fire M3 polygons (current)** 🆓 CA | Canadian fire *footprints* — polygons derived from buffered season-to-date satellite hotspots (NRCan labels them **non-operational** satellite estimates, not surveyed lines). | none | `services/fires.py` → `_ca_perimeters` |
| **NASA FIRMS** 🔑 | Near-real-time satellite thermal *hotspots* (VIIRS aboard NOAA-20), refreshed every few hours. Raw detections, includes fires too new to be in NIFC. | free MAP_KEY | `services/fires.py` (`FIRMS_MAP_KEY`) → `/hotspots/bbox` |

- FIRMS API + free key: https://firms.modaps.eosdis.nasa.gov/api/map_key/
- We query `VIIRS_NOAA20_NRT` over a **2-day** window (NRT for the current day
  lags a few hours, so a 1-day window often misses "yesterday's" detections).
- **US + Canada are fetched concurrently and merged**; if one country's feed fails
  the other still returns.
- **How it feeds the model:** the mapped *perimeter* is the ignition footprint the
  simulation grows from. Map markers are **perimeter-driven** — one dot per drawn
  footprint. US incidents are matched to their perimeter **by name**; Canadian
  incidents (whose CWFIS points and M3 satellite polygons are *separate* datasets
  with no shared ID) are matched **spatially** (size-aware nearest / containment).

## 2. Fuel — what's burning (most important model input)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **LANDFIRE FBFM40 — CONUS** 🆓 US | 30 m raster of Scott & Burgan (2005) **40 fire behavior fuel models** (grass/shrub/timber/slash) plus non-burnable classes (water, urban, rock). The definitive US fuel dataset. | none | `services/fuel.py` (`fuel_at`, `fuel_grid`) |
| **LANDFIRE FBFM40 — Alaska** 🆓 US | The same FBFM40 encoding for **Alaska** (a separate LANDFIRE product from CONUS). Without it, Alaska fires had no fuel data. | none | `services/fuel.py` (auto-selected west of −141°) |
| **CWFIS / CFFDRS FBP Fuel Types** 🆓 CA | Canada's national **Fire Behaviour Prediction** fuel-type grid (boreal conifer C-1..C-7, deciduous D, mixedwood M, grass O-1, plus water/non-fuel), read as a rendered WMS image and colour-decoded. The LANDFIRE-equivalent for Canada. | none | `services/fuel.py` (`fuel_at_ca`, `fuel_grid_ca`) |

- LANDFIRE CONUS: `…/Landfire_LF2022/LF2022_FBFM40_CONUS/ImageServer` · Alaska:
  `…/Landfire_LF2023/LF2023_FBFM40_AK/ImageServer`. CWFIS FBP:
  `cwfis.cfs.nrcan.gc.ca/geoserver/public/wms` layer `cffdrs_fbp_fuel_types_100m`.
- **Fuel source is chosen per fire:** US CONUS or Alaska → LANDFIRE; Canada/elsewhere
  → CWFIS FBP. If a point falls outside all of them, `get_params` falls back to a
  labelled **regional-default** fuel (GR2) so a forecast still runs.
- **Point lookup** (`fuel_at` / `fuel_at_ca`) → the fuel code at the fire, used to
  pick the default fuel and its wind adjustment factor.
- **Domain grid** (`fuel_grid` / `fuel_grid_ca`) → a coarse (~30×30, 2–3 km cells)
  grid across the whole fire domain. Burnable codes pass into ForeFire's FARSITE
  table; **water/urban/rock/no-data become a non-burnable "barrier"** (fuel index 999
  in `fuel_table.py`) so the fire stops at them instead of crossing them.
- **FBP → FBFM40 crosswalk (Canada):** the Canadian FBP classes are translated to the
  closest FBFM40 model ForeFire understands, matched on **surface-fire** behaviour —
  boreal conifer → TL3 (moderate conifer *litter*), *not* a high-load shrub model.
  Boreal's fast **crown**-fire behaviour is added separately by the crown-spotting
  enhancement on dry/windy days, so the surface fuel stays realistic (this avoids a
  runaway over-prediction from mapping boreal to an aggressive timber-shrub model).
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
| **US Census Geocoder** 🆓 US | US street address → lat/lon. Best for full addresses; no city-only matches, US-only. | none | `services/geocoding.py` (tried first) |
| **OpenStreetMap Nominatim (search)** 🆓 US+CA | Free-form search (cities, towns, landmarks) across **US and Canada** (`countrycodes=us,ca`). Fallback so bare place names resolve, and the only source for Canadian queries. | none | `services/geocoding.py` (`geocode`) |
| **OpenStreetMap Nominatim (reverse)** 🆓 | Coordinates → a concise street address. Used to give evacuation destinations (assembly points, computed safe points) a readable address. | none | `services/geocoding.py` (`reverse`) → evacuation |

- Census: https://geocoding.geo.census.gov/geocoder/ · Nominatim: https://nominatim.openstreetmap.org/
- Nominatim's usage policy expects a descriptive User-Agent and low request rates, so
  reverse-geocoding of evacuation destinations is done **sequentially** under a small
  time budget (no parallel bursts).

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
| **Mapbox Directions — `driving-traffic`** 🔑 | Turn-by-turn driving routes **with live traffic** + alternatives + typical-vs-current ETA. Each candidate is filtered so any route crossing the fire/forecast polygon is dropped. | free token | `services/evacuation.py` (`MAPBOX_TOKEN`) |
| **OSRM (public demo server)** 🆓 | Keyless driving-route fallback (real road geometry, **no** live traffic) so routes still render when no Mapbox token is set. Same danger-polygon filtering. | none | `services/evacuation.py` |
| **FEMA National Shelter System — Open Shelters** 🆓 US | Shelters **actually open now**, synced from the **American Red Cross** database (daily + every 20 min). US-only — **skipped in Canada**, where reception centres are announced per-incident by provincial EM / the Red Cross (no equivalent live feed). [gis.fema.gov NSS/OpenShelters](https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/FeatureServer/0) | none | `services/evacuation.py` |
| **OpenStreetMap Overpass** 🆓 | Real named fallbacks near, but clear of, the fire: `emergency=assembly_point`, hospitals, community centres, and **towns/cities/villages** (works US + Canada). (OSM `amenity=shelter` is skipped — mostly picnic/transit shelters.) | none | `services/evacuation.py` |
| **Nominatim reverse** 🆓 | A readable **street address** for every shown destination that lacks one (assembly points, computed points). | none | `services/geocoding.reverse` |
| **Mapbox reverse geocoding** 🔑 | Snaps the geometric fallback points to the **nearest real town** so even the last-resort target has a name. | Mapbox token | `services/evacuation.py` |
| **Computed safe points** 🆓 | Geometric last resort — points a safe distance away in the "away from fire" directions (then town-snapped), so a route target always exists. | none | `services/evacuation.py` |

- Mapbox token (free ~100k req/mo): https://account.mapbox.com/access-tokens/
- **Country-aware:** the FEMA feed and Watch-Duty guidance are used in the US; in
  Canada FEMA is skipped and the panel points to **provincial/territorial emergency
  management or the Canadian Red Cross**. An `emergency=assembly_point` is a real,
  crowd-sourced OSM muster point — named when the mapper tagged a name, otherwise
  labelled "Assembly Point" with a reverse-geocoded address.
- Destinations are a **hybrid**, best-first: open FEMA/Red Cross shelters (US) →
  OSM assembly points / hospitals / community centres / towns → geometric points
  snapped to the nearest town. Without a Mapbox token, `/evacuation` still returns
  drive routes via **OSRM** (no live traffic) plus the safe destinations.

---

## The prediction pipeline (how the pieces combine)

```
mapped perimeter (US NIFC / Canada CWFIS M3)
        │  ignition footprint   (skipped for a fully-controlled fire:
        │                         US 100% contained / Canada "Under Control")
        ▼
gather inputs ──► wind      (NWS → Open-Meteo, HRRR-backed hourly)
        │         moisture  (temp + RH → Simard EMC)
        │         fuel grid (LANDFIRE US CONUS/Alaska OR CWFIS FBP Canada, +barriers)
        │         slope+aspect (Open-Meteo elevation gradient)
        ▼
ForeFire (spawned subprocess): FARSITE surface spread on the fuel/wind/
        │  slope layers, seeded from the perimeter, stepped hour by hour
        ▼
+ crown-fire spotting (dry/windy) · ML area correction · containment credit
        │  (partly-contained fires grow only along their uncontained perimeter)
        ▼
GeoJSON isochrones ──► web map animates the 24 h forecast
```

## The ForeFire model — briefly

`/predict` runs **[ForeFire](https://github.com/forefireAPI/forefire)**, a C++
front-tracking simulator, via its `pyforefire` bindings (see
[FOREFIRE_SETUP.md](FOREFIRE_SETUP.md)). Per request, in a clean subprocess,
`services/forefire_adapter.py`:

- Sizes a local-metre domain around the fire and lays down four layers:
  - **fuel** — the FBFM40 grid (LANDFIRE in the US, CWFIS FBP in Canada), with
    water/urban/rock as non-burnable barriers, against ForeFire's FARSITE fuel table
    (`fuel_table.py`);
  - **wind** — the midflame-reduced forecast wind, re-triggered each step;
  - **slope** — a plane tilted along the real terrain aspect;
  - **moisture** — dead fuel moisture from live temperature/humidity.
- **Ignites** a real `FireFront` traced from the mapped perimeter (US NIFC or Canada
  CWFIS, simplified to the working resolution so the shape is preserved), or a small
  front for a point ignition when no perimeter is nearby.
- **Steps** the model hour by hour with the `Farsite` (Rothermel-family)
  propagation model, exporting each step's front as a GeoJSON `Polygon` tagged with
  `hours`, `head_distance_km`, and `area_km2` — nested isochrones the map animates.
- **Post-processing:** crown-fire ember spotting broadens the surface footprint on
  dry/windy days; a learned **ML area correction** rescales it; and a **containment
  credit** scales the *growth* by the uncontained fraction so a partly-lined fire
  grows only along its uncontained perimeter (a fully-controlled fire is skipped up
  front). The UI also reports the wind's veer/backing and a head-vs-backing
  **directional-spread** read-out.

There is **no fallback engine**: if `pyforefire` is unavailable, `/predict` returns
HTTP 503.

**References:** Rothermel (1972); Andrews (2012, wind adjustment factor);
Simard (1968, EMC); Scott & Burgan (2005, fuel models); Filippi et al. (ForeFire).
This is a research/education tool — **not** operational fire-behavior guidance.
