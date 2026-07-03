"""
Wildfire Map API — FastAPI application entry point.

Run locally:
    cd backend
    pip install -r requirements.txt
    cp .env.example .env        # optional; add a FIRMS key for hotspots
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

Interactive docs at http://localhost:8000/docs
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .routers import fires, geocode, predict, weather

settings = get_settings()

app = FastAPI(
    title="Wildfire Map API",
    version="0.1.0",
    description="Nearby active wildfires + wind-driven spread prediction for the US.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(geocode.router)
app.include_router(fires.router)
app.include_router(weather.router)
app.include_router(predict.router)


@app.get("/health", tags=["meta"])
async def health():
    return {
        "status": "ok",
        "prediction_engine": settings.prediction_engine,
        "firms_configured": bool(settings.firms_map_key),
    }
