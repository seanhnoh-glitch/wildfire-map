from fastapi import APIRouter, HTTPException, Query

from ..schemas import EvacuationRequest, EvacuationResponse, Fire, NearbyFiresResponse
from ..services import evacuation as evac_svc
from ..services import fires as fires_svc

router = APIRouter(tags=["fires"])


@router.get("/fires/all", response_model=list[Fire])
async def all_fires(
    min_acres: float = Query(10.0, ge=0, description="Only fires at/above this size"),
    limit: int = Query(2000, gt=0, le=5000),
):
    """Every ongoing US + Canadian wildfire (points only), largest first — for the overview map."""
    try:
        return await fires_svc.all_active(min_acres=min_acres, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fire data source error: {exc}")


@router.get("/perimeters/all")
async def all_perimeters(
    min_acres: float = Query(100.0, ge=0, description="Only perimeters at/above this size"),
    limit: int = Query(1500, gt=0, le=3000),
):
    """All current US + Canadian fire perimeters (simplified) as a GeoJSON FeatureCollection."""
    try:
        return await fires_svc.all_perimeters(min_acres=min_acres, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Perimeter data source error: {exc}")


@router.get("/perimeters/bbox")
async def perimeters_bbox(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    min_acres: float = Query(10.0, ge=0),
    offset: float = Query(0.0, ge=0, description="maxAllowableOffset in degrees; 0 = full res"),
):
    """Full-resolution perimeters within the current viewport, for crisp zoomed-in detail."""
    try:
        return await fires_svc.perimeters_in_bbox(west, south, east, north, min_acres=min_acres, offset=offset)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Perimeter data source error: {exc}")


@router.get("/perimeters/at")
async def perimeter_at(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
):
    """Whether the fire at this point has an official mapped perimeter (so its spread
    can be forecast). Mirrors the /predict no-perimeter guard so the UI's forecast
    button agrees with what a forecast would actually do."""
    try:
        has = await fires_svc.has_perimeter_near(lat, lon)
    except Exception:
        has = False
    return {"has_perimeter": has}


@router.get("/hotspots/bbox")
async def hotspots_bbox(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
):
    """NASA FIRMS thermal hotspots within the current viewport (GeoJSON points).
    Empty if no FIRMS key is configured."""
    try:
        return await fires_svc.hotspots_in_bbox(west, south, east, north)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Hotspot data source error: {exc}")


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


@router.post("/evacuation", response_model=EvacuationResponse)
async def evacuation(req: EvacuationRequest):
    """
    Traffic-aware evacuation routes leading away from a fire to a safe destination.

    Pass the fire's forecast spread (the /predict `isochrones`) as `avoid_geojson`
    so routes avoid where the fire is *going*, not just where it is now. Needs
    MAPBOX_TOKEN for live-traffic driving directions; without it, safe destinations
    are still returned so the client can show them.
    """
    try:
        return await evac_svc.plan(
            lat=req.lat, lon=req.lon,
            fire_lat=req.fire_lat, fire_lon=req.fire_lon,
            avoid_geojson=req.avoid_geojson, max_routes=req.max_routes,
            country=req.country,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Evacuation routing error: {exc}")
