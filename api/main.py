"""FastAPI application entry point."""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from api.database import create_tables
from api.routers import (
    auth,
    credentials,
    dashboard,
    global_market,
    indicators,
    quick_trade,
    strategies,
    templates,
    users,
    watchlist,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Rate limiter (shared across routers) ──────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="KIS Trading API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────────────────
# Never combine "*" with credentials (rejected by browsers). Origins come from CORS_ORIGINS
# (wired in docker-compose/.env on this branch); CORS_ALLOWED_ORIGINS is accepted as a
# compatibility fallback (main's name). Defaults to localhost dev origins so production must
# set origins explicitly (e.g. CORS_ORIGINS=https://app.example.com,capacitor://localhost).
_cors_env = (
    os.environ.get("CORS_ORIGINS")
    or os.environ.get("CORS_ALLOWED_ORIGINS")
    or "http://localhost:3000,http://localhost:5173"
)
_ALLOWED_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()]
if "*" in _ALLOWED_ORIGINS:
    raise RuntimeError(
        "CORS_ORIGINS must not contain '*' when allow_credentials=True. "
        "Set it to your specific mobile/web origins "
        "(e.g. CORS_ORIGINS=https://app.example.com,capacitor://localhost)"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
)


# ── Global exception handler ──────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"code": -1, "data": None, "msg": str(exc)},
    )


# ── Startup ───────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    logger.info("Creating database tables…")
    try:
        create_tables()
        logger.info("Database tables ready.")
    except Exception as e:
        logger.error("Failed to create tables: %s", e)


# ── Health check ─────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(credentials.router)
app.include_router(dashboard.router)
app.include_router(strategies.router)
app.include_router(templates.router)
app.include_router(indicators.router)
app.include_router(watchlist.router)
app.include_router(global_market.router)
app.include_router(users.router)
app.include_router(quick_trade.router)
