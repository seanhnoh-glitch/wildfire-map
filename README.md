# Wildfire Map 🔥🗺️

An interactive mobile map for the US where you enter your address (or use GPS),
see **active wildfires near you**, tap one, and view a **forecast of where it's
predicted to spread** over the next hours — driven by live wind, fuel, and terrain.

- **Backend:** FastAPI service that aggregates live fire, weather, fuel, and
  terrain data and runs the spread prediction.
- **Mobile:** React Native (Expo) app rendering everything on a MapLibre map.
- **Prediction:** a built-in wind-driven **elliptical spread model** (works out of
  the box), with a **ForeFire** engine adapter as the upgrade path.

> ⚠️ Research/education project. Forecasts are **not** operational fire-behavior
> guidance. In a real emergency follow official sources (InciWeb, Watch Duty,
> local authorities).

## What's live vs. what's a stub

| Feature | Status |
|---|---|
| Address geocoding (US Census) | ✅ live |
| Nearby active fires (NIFC WFIGS points + perimeters) | ✅ live |
| Satellite hotspots (NASA FIRMS) | ✅ live *(needs a free key)* |
| Live weather / wind (NWS → Open-Meteo) | ✅ live |
| Fuel model at a point (LANDFIRE) + Scott & Burgan params | ✅ live |
| Slope estimate (Open-Meteo elevation) | ✅ live |
| Spread forecast → GeoJSON isochrones (built-in model) | ✅ live |
| ForeFire engine | 🔌 adapter wired, NetCDF builder pending — see `docs/FOREFIRE_SETUP.md` |
| HRRR forecast wind (time-evolving spread) | ⏳ documented, not yet wired |

Full source list: **[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)**.

## Quickstart

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows:  .venv\Scripts\activate     macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # optional; add FIRMS_MAP_KEY for hotspots
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/docs for interactive API docs. Quick checks:

```bash
curl "http://localhost:8000/geocode?address=1600+Pennsylvania+Ave+NW,+Washington,+DC"
curl "http://localhost:8000/fires/nearby?lat=39.5&lon=-121.6&radius_km=200"
curl -X POST http://localhost:8000/predict -H "Content-Type: application/json" \
     -d '{"lat":39.5,"lon":-121.6,"duration_hours":6,"step_minutes":60}'
```

### 2. Mobile app

The app uses MapLibre native modules, so it needs a **dev build** (not Expo Go):

```bash
cd mobile
npm install
npx expo prebuild            # generates native android/ios projects
npx expo run:android         # or: npx expo run:ios   (needs Xcode/Android Studio)
```

Point the app at your backend by setting the API base URL — see
`mobile/src/lib/config.ts` (Android emulator uses `10.0.2.2`; a physical phone
needs your laptop's LAN IP, e.g. `EXPO_PUBLIC_API_URL=http://192.168.1.42:8000`).

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/geocode?address=` | Address → lat/lon |
| GET | `/fires/nearby?lat=&lon=&radius_km=` | Active fires + perimeters + hotspots |
| GET | `/weather?lat=&lon=` | Current wind/temp/RH at a point |
| POST | `/predict` | Spread forecast → GeoJSON isochrones |
| GET | `/health` | Status + which engine is active |

## Architecture

```
mobile (Expo + MapLibre)
   │  REST/JSON
   ▼
backend (FastAPI)
   ├─ routers/         thin HTTP layer
   └─ services/
        geocoding.py   US Census
        fires.py       NIFC WFIGS + NASA FIRMS
        weather.py     NWS + Open-Meteo
        fuel.py        LANDFIRE + Scott&Burgan params
        terrain.py     Open-Meteo elevation → slope
        spread_model.py    built-in elliptical model
        forefire_adapter.py  input gathering + engine selection (+ ForeFire seam)
```

See `docs/` for the data-source reference, the spread-model math, and the
ForeFire wiring guide.

## Roadmap

1. Time-evolving forecast using HRRR wind fields.
2. ForeFire NetCDF landscape builder (LANDFIRE + 3DEP clips) — `docs/FOREFIRE_SETUP.md`.
3. Ignite from mapped perimeters instead of a single point.
4. Push notifications when a new fire appears inside a saved radius.
