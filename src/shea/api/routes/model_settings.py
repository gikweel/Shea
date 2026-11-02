from __future__ import annotations

from typing import Annotated, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request

from shea.api.contracts import ModelSettingsUpdate, ModelSettingsView
from shea.bootstrap import SheaRuntime
from shea.model.settings import PublicModelSettings

router = APIRouter(prefix="/model-settings", tags=["Model settings"])


def get_runtime(request: Request) -> SheaRuntime:
    return cast(SheaRuntime, request.app.state.runtime)


def _require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    origin_url = urlsplit(origin)
    if (
        origin_url.scheme != request.url.scheme
        or origin_url.netloc.lower() != request.url.netloc.lower()
    ):
        raise HTTPException(status_code=403, detail="Cross-origin model settings changes are blocked.")


def _to_view(settings: PublicModelSettings) -> ModelSettingsView:
    return ModelSettingsView(
        provider=settings.provider,
        base_url=settings.base_url,
        model_name=settings.model_name,
        configured=settings.configured,
        api_key_configured=settings.api_key_configured,
    )


@router.get("/", response_model=ModelSettingsView)
async def get_model_settings(
    runtime: Annotated[SheaRuntime, Depends(get_runtime)],
) -> ModelSettingsView:
    return _to_view(runtime.model_settings_service.public_settings())


@router.put("/", response_model=ModelSettingsView)
async def save_model_settings(
    payload: ModelSettingsUpdate,
    runtime: Annotated[SheaRuntime, Depends(get_runtime)],
    request: Request,
) -> ModelSettingsView:
    _require_same_origin(request)
    try:
        settings = runtime.model_settings_service.save(
            provider=payload.provider,
            base_url=payload.base_url,
            model_name=payload.model_name,
            api_key=payload.api_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_view(settings)


@router.delete("/", response_model=ModelSettingsView)
async def clear_model_settings(
    runtime: Annotated[SheaRuntime, Depends(get_runtime)],
    request: Request,
) -> ModelSettingsView:
    _require_same_origin(request)
    settings = runtime.model_settings_service.clear()
    return _to_view(settings)