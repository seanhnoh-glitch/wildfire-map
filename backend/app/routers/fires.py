from fastapi import APIRouter, HTTPException, Query

from ..schemas import Fire, NearbyFiresResponse
from ..services import fires as fires_svc

router = APIRouter(tags=["fires"])


@router.get("/fires/all", response_model=list[Fire])
async def all_fires(
    min_acres: float = Query(10.0, ge=0, description="Only fires at/above this size"),
    limit: int = Query(2000, gt=0, le=5000),
):
    """Every ongoing US wildfire (points only), largest first — for the overview map."""
    try:
        return await fires_svc.all_active(min_acres=min_acres, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire data source error: {exc}")


@router.get("/perimeters/all")
async def all_perimeters(
    min_acres: float = Query(100.0, ge=0, description="Only perimeters at/above this size"),
    limit: int = Query(1500, gt=0, le=3000),
):
    """All current US fire perimeters (simplified) as a GeoJSON FeatureCollection."""
    try:
        return await fires_svc.all_perimeters(min_acres=min_acres, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Perimeter data source error: {exc}")


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
