from fastapi import APIRouter, HTTPException

from ..schemas import PredictRequest, PredictResponse
from ..services import forefire_adapter

router = APIRouter(tags=["predict"])


@router.post("/predict", response_model=PredictResponse)
async def predict_spread(req: PredictRequest):
    """
    Forecast a fire's spread from an ignition/current point over the next
    `duration_hours`, returning nested GeoJSON isochrones for the map to animate.
    """
    try:
        return await forefire_adapter.predict(req)
    except forefire_adapter.ForeFireUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Prediction failed: {exc}")
