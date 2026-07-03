from fastapi import APIRouter, HTTPException, Query

from ..schemas import WeatherConditions
from ..services import weather as weather_svc

router = APIRouter(tags=["weather"])


@router.get("/weather", response_model=WeatherConditions)
async def weather_at(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    try:
        return await weather_svc.current(lat, lon)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Weather source error: {exc}")
