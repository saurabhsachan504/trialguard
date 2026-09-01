"""FastAPI application entry point."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import engine, init_db
from app.routers import admin, auth, billing, ollama_proxy, summarize, usage, webhooks

STATIC_DIR = Path(__file__).parent / "static"

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("trialguard")

_INSECURE_DEFAULTS = {
    "dev-only-insecure-secret-change-me",
    "dev-only-insecure-device-pepper-change-me",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.ENV == "prod":
        if settings.SECRET_KEY in _INSECURE_DEFAULTS or (
            settings.DEVICE_HASH_SECRET in _INSECURE_DEFAULTS
        ):
            raise RuntimeError(
                "SECRET_KEY and DEVICE_HASH_SECRET must be set to strong random "
                "values before running in production."
            )
        if settings.PAYMENT_PROVIDER == "mock":
            logger.warning("PAYMENT_PROVIDER=mock in production - no money will move.")
    else:
        # Convenient for dev/tests; production should run Alembic migrations.
        init_db()
    yield
    engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description=(
        "Registration, device-bound free trials and $5/month subscriptions "
        "for a Chrome extension."
    ),
    lifespan=lifespan,
    docs_url="/docs" if settings.ENV != "prod" else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,  # we use Authorization headers, not cookies
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Admin-Key"],
    max_age=600,
)


@app.middleware("http")
async def add_timing_and_security_headers(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid request.", "errors": exc.errors()},
    )


app.include_router(auth.router, prefix=settings.API_PREFIX)
app.include_router(usage.router, prefix=settings.API_PREFIX)
app.include_router(summarize.router, prefix=settings.API_PREFIX)
app.include_router(billing.router, prefix=settings.API_PREFIX)
app.include_router(webhooks.router, prefix=settings.API_PREFIX)
app.include_router(admin.router, prefix=settings.API_PREFIX)
app.include_router(ollama_proxy.router, prefix=settings.API_PREFIX)


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "env": settings.ENV, "version": app.version}


@app.get(f"{settings.API_PREFIX}/meta", tags=["meta"])
def meta():
    return {
        "name": settings.APP_NAME,
        "docs": "/docs" if settings.ENV != "prod" else None,
        "api": settings.API_PREFIX,
        "free_trials": settings.FREE_TRIAL_LIMIT,
        "price": f"${settings.PLAN_PRICE_CENTS / 100:.2f}/{settings.PLAN_INTERVAL}",
        # Frontend ko batata hai ki Google button dikhana hai ya nahi.
        "google_login": settings.GOOGLE_LOGIN_ENABLED and bool(settings.GOOGLE_CLIENT_ID),
        "google_client_id": settings.GOOGLE_CLIENT_ID if settings.GOOGLE_LOGIN_ENABLED else "",
    }


if settings.WEB_APP_ENABLED and STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(STATIC_DIR / "index.html")

else:  # pragma: no cover - API-only deployment

    @app.get("/", tags=["meta"])
    def root():
        return meta()
