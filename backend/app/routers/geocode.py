from fastapi import APIRouter, HTTPException, Query

from ..schemas import GeocodeResult
from ..services import geocoding

router = APIRouter(tags=["geocode"])


@router.get("/geocode", response_model=GeocodeResult)
async def geocode_address(address: str = Query(..., min_length=3, description="US or Canadian address or city")):
    try:
        result = await geocoding.geocode(address)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Geocoder error: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail="No match for that address. Try a full street address.")
    return result
