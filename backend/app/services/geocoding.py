"""
Address / place -> coordinates.

Two free, keyless sources, tried in order so both precise street addresses AND
bare city/place names work across the US AND Canada:

  1. US Census geocoder — authoritative for full US street addresses, but returns
     NO match for city-only queries like "Redding, CA" and nothing outside the US.
  2. OpenStreetMap Nominatim — free-form search that resolves cities, towns,
     landmarks, and addresses. Used as the fallback and the ONLY source for Canada
     (the Census geocoder is US-only), scoped to US + Canada via countrycodes.

For higher volume / autocomplete, swap in Mapbox or Google behind `geocode()`.
Nominatim's usage policy expects a descriptive User-Agent and low request rates.
"""
import httpx

from ..schemas import GeocodeResult

CENSUS_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
UA = "WildfireMap/0.1 (wildfire-map prototype)"


def _short_address(addr: dict) -> str | None:
    """Build a concise 'house road, city, state' string from Nominatim address parts."""
    road = " ".join(x for x in (addr.get("house_number"), addr.get("road")) if x)
    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("hamlet")
    parts = [p for p in (road, city, addr.get("state")) if p]
    return ", ".join(parts) or None


async def reverse(lat: float, lon: float, client: httpx.AsyncClient | None = None) -> str | None:
    """Coordinates -> a concise human address via Nominatim reverse geocoding. Best-
    effort: returns None on any failure. An existing AsyncClient can be passed to reuse
    the connection when reverse-geocoding several points."""
    params = {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18, "addressdetails": 1}

    async def _do(c: httpx.AsyncClient) -> dict:
        resp = await c.get(NOMINATIM_REVERSE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    try:
        if client is not None:
            data = await _do(client)
        else:
            async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": UA}) as c:
                data = await _do(c)
    except Exception:
        return None
    return _short_address(data.get("address") or {}) or data.get("display_name")


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
    params = {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us,ca", "addressdetails": 0}
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
