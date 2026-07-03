from fastapi import APIRouter, HTTPException, Query

from ..schemas import NearbyFiresResponse
from ..services import fires as fires_svc

router = APIRouter(tags=["fires"])


@router.get("/fires/nearby", response_model=NearbyFiresResponse)
async def nearby_fires(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    radius_km: float = Query(80.0, gt=0, le=500),
):
    try:
        data = await fires_svc.nearby(lat, lon, radius_km)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire data source error: {exc}")
    return NearbyFiresResponse(
        query={"lat": lat, "lon": lon},
        radius_km=radius_km,
        count=len(data["fires"]),
        fires=data["fires"],
        hotspots=data["hotspots"],
        perimeters=data["perimeters"],
    )
