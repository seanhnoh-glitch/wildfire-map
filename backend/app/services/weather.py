"""
Current weather at a point, used both for display and to drive the spread model.

Primary source: NWS api.weather.gov (US, authoritative, no key). If the NWS
lookup fails (it needs a two-step gridpoint resolution and occasionally 500s),
we fall back to Open-Meteo, which is global, free, and keyless.

Wind direction is normalized to the meteorological convention: the direction the
wind blows FROM, in degrees (0=N, 90=E).
"""
import httpx

from ..schemas import WeatherConditions

NWS_POINTS = "https://api.weather.gov/points/{lat},{lon}"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


async def _nws(client: httpx.AsyncClient, lat: float, lon: float) -> WeatherConditions | None:
    meta = await client.get(NWS_POINTS.format(lat=round(lat, 4), lon=round(lon, 4)))
    meta.raise_for_status()
    obs_stations_url = meta.json()["properties"]["observationStations"]

    stations = await client.get(obs_stations_url)
    stations.raise_for_status()
    features = stations.json().get("features", [])
    if not features:
        return None
    station_id = features[0]["properties"]["stationIdentifier"]

    latest = await client.get(f"https://api.weather.gov/stations/{station_id}/observations/latest")
    latest.raise_for_status()
    p = latest.json()["properties"]

    def val(key):
        v = p.get(key) or {}
        return v.get("value")

    wind_ms = val("windSpeed")
    gust_ms = val("windGust")
    return WeatherConditions(
        source=f"NWS ({station_id})",
        time=p.get("timestamp"),
        temperature_c=val("temperature"),
        relative_humidity=val("relativeHumidity"),
        wind_speed_kmh=None if wind_ms is None else wind_ms * 3.6,
        wind_direction_deg=val("windDirection"),
        wind_gust_kmh=None if gust_ms is None else gust_ms * 3.6,
    )


async def _open_meteo(client: httpx.AsyncClient, lat: float, lon: float) -> WeatherConditions:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "wind_speed_unit": "kmh",
    }
    resp = await client.get(OPEN_METEO, params=params)
    resp.raise_for_status()
    cur = resp.json().get("current", {})
    return WeatherConditions(
        source="Open-Meteo",
        time=cur.get("time"),
        temperature_c=cur.get("temperature_2m"),
        relative_humidity=cur.get("relative_humidity_2m"),
        wind_speed_kmh=cur.get("wind_speed_10m"),
        wind_direction_deg=cur.get("wind_direction_10m"),
        wind_gust_kmh=cur.get("wind_gusts_10m"),
    )


async def current(lat: float, lon: float) -> WeatherConditions:
    async with httpx.AsyncClient(
        timeout=20.0,
        headers={"User-Agent": "WildfireMap/0.1 (contact: you@example.com)", "Accept": "application/geo+json"},
    ) as client:
        try:
            result = await _nws(client, lat, lon)
            if result and result.wind_speed_kmh is not None:
                return result
        except Exception:
            pass
        return await _open_meteo(client, lat, lon)
