# Data Sources & APIs

Every external source this project uses (or is wired to use), what it provides,
whether it needs a key, and how it feeds the model. All are US-focused, free, and
public. This is the reference list for the four model ingredients — **fire
location, fuel, terrain, weather** — plus geocoding and map tiles.

Legend: 🔑 needs a (free) key · 🆓 no key · ⏳ wired but not yet fully implemented

---

## 1. Where the fire is (ignition + display)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **NIFC WFIGS — Incident Locations (Current)** 🆓 | Authoritative current US wildfire *points*: name, size (acres), % contained, discovery time, county/state. New fires appear here fastest. | none | `services/fires.py` → `/fires/nearby` |
| **NIFC WFIGS — Interagency Perimeters (Current)** 🆓 | Mapped fire *footprints* (polygons). Lags 12–24 h behind the point feed. | none | `services/fires.py` (perimeters + ignition seed) |
| **NASA FIRMS** 🔑 | Near-real-time satellite thermal *hotspots* (VIIRS/MODIS), ~every few hours. Raw detections, includes fires too new to be in NIFC. | free MAP_KEY | `services/fires.py` (`FIRMS_MAP_KEY`) |

- WFIGS points ArcGIS endpoint: `services3.arcgis.com/T4QMspbfLg3qTGWY/.../WFIGS_Incident_Locations_Current/FeatureServer/0`
- FIRMS API + free key: https://firms.modaps.eosdis.nasa.gov/api/area/
- **How it feeds the model:** the nearest incident point (or its perimeter) is the *ignition* the spread model grows from.

## 2. Fuel — what's burning (most important model input)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **LANDFIRE FBFM40** 🆓 | 30 m US raster of Scott & Burgan (2005) **40 fire behavior fuel models**, plus canopy cover/height/bulk density and vegetation. The definitive US fuel dataset. | none | `services/fuel.py` (`fuel_at()`), ForeFire landscape |
| **Scott & Burgan fuel params** (built-in) | Crosswalk from each fuel code → reference rate-of-spread + wind response, consumed by the built-in model. Approximate, literature-informed. | n/a | `services/fuel.py` (`FUEL_MODELS`) |

- LANDFIRE: https://landfire.gov/ · ArcGIS/ImageServer identify used for point lookup.
- **How it feeds the model:** the fuel code at the fire sets the base spread rate and how strongly wind accelerates it. For ForeFire, a LANDFIRE clip becomes the fuel grid of the NetCDF landscape and maps to `fuels.ff`.

## 3. Terrain — slope drives uphill spread

| Source | Provides | Key | Used in |
|---|---|---|---|
| **Open-Meteo Elevation** 🆓 | Point elevation (Copernicus DEM), used to estimate local slope by sampling a small cross. | none | `services/terrain.py` |
| **USGS 3DEP** ⏳ | 1–10 m US elevation (DEM). For a full ForeFire pipeline you clip a 3DEP tile as the elevation grid. | none | (ForeFire landscape — see FOREFIRE_SETUP.md) |
| **OpenTopography** ⏳ | Programmatic DEM clips (SRTM/3DEP) via REST. Alternative to The National Map. | free key for some | (terrain clip) |

- USGS 3DEP / The National Map: https://apps.nationalmap.gov/ · OpenTopography: https://opentopography.org/
- **How it feeds the model:** steeper slope → higher head rate of spread (fire runs uphill).

## 4. Weather — wind is the #1 dynamic driver

| Source | Provides | Key | Used in |
|---|---|---|---|
| **NWS api.weather.gov** 🆓 | Official US current conditions: wind speed/direction/gust, temp, RH. | none | `services/weather.py` (`current`, primary) |
| **Open-Meteo Forecast** 🆓 | Global current + **hourly forecast** wind/temp/RH. `current` fallback AND the source of hourly forecast wind. | none | `services/weather.py` (`current` fallback, `forecast_hourly`) |
| **NOAA HRRR (via Open-Meteo)** ✅ | 3 km hourly wind *forecast* — the time-evolving driver. Open-Meteo's `best_match` uses HRRR for short-range US, so `forecast_hourly` is HRRR-quality without GRIB parsing. Pin explicitly with `models=ncep_hrrr_conus`. | none | `services/weather.py` (`forecast_hourly`) → time-varying spread |
| **NOAA HRRR raw grids (NOMADS)** ⏳ | Direct GRIB2 HRRR for higher *spatial* resolution (a wind field, not one point). Needs cfgrib/Herbie. | none | (future: spatial wind field) |
| **Synoptic / MesoWest (RAWS)** ⏳ | Real-time observed wind from ground stations near the fire. | free tier key | (future: nearest-station wind) |

- NWS: https://www.weather.gov/documentation/services-web-api · Open-Meteo: https://open-meteo.com/
- HRRR via NOMADS: https://nomads.ncep.noaa.gov/ (or the `Herbie` Python library)
- **How it feeds the model:** wind speed sets head spread rate + ellipse elongation; wind direction sets which way the fire is pushed. Weather reports the direction wind blows *from*; the model pushes the fire the opposite way.

## 5. Address → coordinates (geocoding)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **US Census Geocoder** 🆓 | US street address → lat/lon. No key, no rate hassle. | none | `services/geocoding.py` → `/geocode` |
| **Mapbox / Google Geocoding** ⏳ | Better autocomplete + global coverage. Paid tiers / license terms. | key | (optional swap, same interface) |

- Census: https://geocoding.geo.census.gov/geocoder/

## 6. Map tiles (the base map)

| Source | Provides | Key | Used in |
|---|---|---|---|
| **OpenStreetMap raster** 🆓 | Keyless base map for development. **Not** for production traffic (usage policy). | none | `mobile/src/lib/mapStyle.ts` |
| **MapTiler / Stadia / self-hosted** ⏳ | Production vector/raster tiles, satellite imagery. | key | (swap `tiles` URL) |

- Rendering: **MapLibre** (`@maplibre/maplibre-react-native`) — free, no per-tile billing.

---

## The prediction pipeline (how the pieces combine)

```
ignition point (NIFC/FIRMS)
        │
        ▼
gather inputs ──► wind      (NWS → Open-Meteo)
        │         fuel      (LANDFIRE → Scott&Burgan params)
        │         slope     (Open-Meteo elevation)
        ▼
engine select (config.PREDICTION_ENGINE)
   ├── ForeFire (when installed): build NetCDF landscape → simulate → fronts
   └── built-in elliptical model (default): wind+fuel+slope → nested ellipses
        │
        ▼
GeoJSON isochrones ──► phone map animates the forecast
```

## Built-in model — the math, briefly

The fallback/baseline engine (`services/spread_model.py`) is a documented
**elliptical fire-growth model**:

- **Head rate of spread** `R_head = R0 · (1 + φ_wind + φ_slope)`, where `R0` is a
  fuel-specific no-wind baseline, `φ_wind` grows with wind^1.5 scaled by the
  fuel's wind response, and `φ_slope` grows with steepness. Rothermel-inspired
  multiplicative form.
- **Shape:** the front is an ellipse with the ignition point at the rear focus;
  its length-to-breadth ratio grows with wind speed (Alexander 1985 form),
  bounded to [1, 8].
- Each forecast step is one nested ellipse, exported as a GeoJSON polygon tagged
  with elapsed hours, head distance, and burned area.

**Time-varying forecast (`simulate_timevarying`).** The default `/predict` path
does *not* assume one fixed wind. It grows the perimeter incrementally: at each
hourly step, every point on the front advances outward by that hour's local
spread rate (elliptical polar form — fastest downwind, slowest backing). Feeding
the **HRRR-backed hourly wind series** into this makes the fire genuinely **bend
as the wind shifts**, the same Huygens-wavelet approach FARSITE uses
(Anderson 1983; Richards 1990). Supply an explicit `wind_speed_kmh` /
`wind_direction_deg` to hold wind constant instead, or set
`use_forecast_wind=false`.

**References:** Rothermel (1972); Anderson (1983); Alexander (1985);
Scott & Burgan (2005). This is a research/education tool — **not** operational
fire-behavior guidance.
```
