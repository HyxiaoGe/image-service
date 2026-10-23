from fastapi import APIRouter

from src.api.billing import router as billing_router
from src.api.generate import router as generate_router
from src.api.health import router as health_router
from src.api.models import router as models_router
from src.api.usage import router as usage_router

v1_router = APIRouter()
v1_router.include_router(generate_router)
v1_router.include_router(models_router)
v1_router.include_router(health_router)
v1_router.include_router(usage_router)
v1_router.include_router(billing_router)
