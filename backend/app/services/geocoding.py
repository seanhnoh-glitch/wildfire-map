"""
Address / place -> coordinates.

Two free, keyless sources, tried in order so both precise street addresses AND
bare city/place names work:

  1. US Census geocoder — authoritative for full US street addresses, but returns
     NO match for city-only queries like "Redding, CA".
  2. OpenStreetMap Nominatim — free-form search that resolves cities, towns,
     landmarks, and addresses. Used as the fallback.

For higher volume / autocomplete, swap in Mapbox or Google behind `geocode()`.
Nominatim's usage policy expects a descriptive User-Agent and low request rates.
"""
import httpx

from ..schemas import GeocodeResult

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = "WildfireMap/0.1 (wildfire-map prototype)"


async def _census(client: httpx.AsyncClient, address: str) -> GeocodeResult | None:
    params = {"address": address, "benchmark": "Public_AR_Current", "format": "json"}
    resp = await client.get(CENSUS_URL, params=params)
    resp.raise_for_status()
    matches = resp.json().get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    top = matches[0]
    c = top["coordinates"]
    return GeocodeResult(lat=c["y"], lon=c["x"], label=top["matchedAddress"])


async def _nominatim(client: httpx.AsyncClient, address: str) -> GeocodeResult | None:
    params = {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us", "addressdetails": 0}
    resp = await client.get(NOMINATIM_URL, params=params)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    top = results[0]
    return GeocodeResult(lat=float(top["lat"]), lon=float(top["lon"]), label=top.get("display_name", address))


async def geocode(address: str) -> GeocodeResult | None:
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": UA}) as client:
        # Census first (best for street addresses); ignore its transport errors and
        # fall through to Nominatim, which also handles cities/places.
        try:
            result = await _census(client, address)
            if result:
                return result
        except Exception:
            pass
        return await _nominatim(client, address)
