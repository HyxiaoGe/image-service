from __future__ import annotations

from fastapi import APIRouter

from src.config import SUPPORTED_MODELS
from src.models.response import ModelInfoResponse, ModelsListResponse

router = APIRouter()


@router.get("/models", response_model=ModelsListResponse)
async def list_models() -> ModelsListResponse:
    models = [
        ModelInfoResponse(
            id=m.id,
            name=m.name,
            provider=m.provider,
            aspect_ratios=list(m.aspect_ratios) if m.aspect_ratios is not None else None,
            grounding=m.grounding,
        )
        for m in SUPPORTED_MODELS.values()
    ]
    return ModelsListResponse(models=models)
