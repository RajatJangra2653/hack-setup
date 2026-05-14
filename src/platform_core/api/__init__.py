"""FastAPI application factory and API layer.

Provides:
  - App factory with middleware, CORS, error handling
  - Dependency injection via FastAPI Depends
  - Versioned router mounting
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from platform_core import __version__
from platform_core.core.errors import PlatformError, NotFoundError, ConflictError, ValidationError
from platform_core.telemetry import new_correlation_id, set_context


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown lifecycle hooks."""
    # Startup
    from platform_core.events import get_event_bus
    from platform_core.workers import get_worker_registry

    await get_worker_registry().start_all()
    yield
    # Shutdown
    await get_worker_registry().stop_all()


def create_app(*, title: str = "Hackathon Platform API", debug: bool = False) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title=title,
        version=__version__,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
        debug=debug,
    )

    # ── CORS ─────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tightened per-env via config
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Correlation ID middleware ─────────────────────────────────
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = request.headers.get("X-Correlation-ID", "")
        if not cid:
            cid = new_correlation_id()
        else:
            set_context(correlation_id=cid)
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        return response

    # ── Error handlers ───────────────────────────────────────────
    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"error": str(exc), "type": "not_found"})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(status_code=409, content={"error": str(exc), "type": "conflict"})

    @app.exception_handler(ValidationError)
    async def validation_handler(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content={"error": str(exc), "type": "validation"})

    @app.exception_handler(PlatformError)
    async def platform_error_handler(request: Request, exc: PlatformError):
        return JSONResponse(status_code=500, content={"error": str(exc), "type": "platform_error"})

    # ── Mount routers ────────────────────────────────────────────
    from platform_core.api.routers import hacks, users, reconciliation, operations, audit, inventory, scheduler

    app.include_router(hacks.router, prefix="/api/v1")
    app.include_router(users.router, prefix="/api/v1")
    app.include_router(reconciliation.router, prefix="/api/v1")
    app.include_router(operations.router, prefix="/api/v1")
    app.include_router(audit.router, prefix="/api/v1")
    app.include_router(inventory.router, prefix="/api/v1")
    app.include_router(scheduler.router, prefix="/api/v1")

    # ── Health ───────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {"status": "healthy", "version": __version__}

    @app.get("/ready")
    async def readiness():
        return {"status": "ready"}

    return app
