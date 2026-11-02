from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from shea.api.routes import (
    credentials,
    interaction,
    memory,
    model_settings,
    observability,
    profiles,
    tasks,
    tools,
)
from shea.bootstrap import build_runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage the lifecycle of the SheaRuntime."""
    # Build the runtime when the server starts
    db_path = os.environ.get("SHEA_DB_PATH", "shea.db")
    workspace = os.environ.get("SHEA_WORKSPACE", "./workspace")
    
    runtime = build_runtime(db_path=db_path, workspace=workspace, include_http_fetch=True)
    runtime.model_settings_service.load_saved()
    app.state.runtime = runtime
    
    yield  # Server runs here
    
    # Optional cleanup on shutdown
    pass


def create_app() -> FastAPI:
    """Factory to create the FastAPI application."""
    app = FastAPI(
        title="Shea Agent API",
        description="Unified backend for the Shea Web and Desktop GUIs.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Allow origins: in production set SHEA_ALLOWED_ORIGINS=https://my.app.com
    # In dev, defaults to wildcard for convenience.
    raw_origins = os.environ.get("SHEA_ALLOWED_ORIGINS", "*")
    cors_origins = [o.strip() for o in raw_origins.split(",")] if raw_origins != "*" else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount API routers
    app.include_router(interaction.router, prefix="/api")
    app.include_router(memory.router, prefix="/api")
    app.include_router(observability.router, prefix="/api")
    app.include_router(credentials.router, prefix="/api")
    app.include_router(model_settings.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.include_router(profiles.router, prefix="/api")
    app.include_router(tools.router, prefix="/api")

    # Register the frontend GUI from the gui/web folder at the root
    gui_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "gui", "web")
    app.mount("/", StaticFiles(directory=gui_path, html=True), name="gui")

    return app


if __name__ == "__main__":
    import uvicorn
    # When run directly, start the server
    uvicorn.run(
        "shea.api.server:create_app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        factory=True
    )