"""
Address -> coordinates using the free US Census geocoder (no API key, US only).

Docs: https://geocoding.geo.census.gov/geocoder/
If you later want autocomplete / global coverage, swap this for Mapbox or Google
behind the same `geocode()` signature.
"""
import httpx

from ..schemas import GeocodeResult

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


async def geocode(address: str) -> GeocodeResult | None:
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "WildfireMap/0.1"}) as client:
        resp = await client.get(CENSUS_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    matches = data.get("result", {}).get("addressMatches", [])
    if not matches:
        return None
    top = matches[0]
    coords = top["coordinates"]
    return GeocodeResult(lat=coords["y"], lon=coords["x"], label=top["matchedAddress"])
