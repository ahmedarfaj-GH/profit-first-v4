from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.routes_entities import router as api_entities_router
from app.api.routes_runs import router as api_runs_router
from app.auth import bootstrap_platform_admin
from app.config import is_production, validate_config
from app.db import get_db, init_db
from app.ui.routes import router as ui_router

BASE_DIR = Path(__file__).resolve().parent
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

validate_config()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    bootstrap_platform_admin()
    yield


app = FastAPI(
    title="Profit First",
    version="0.2.0",
    docs_url=None if is_production() else "/docs",
    redoc_url=None,
    openapi_url=None if is_production() else "/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    # Default handler echoes the rejected input back, which crashes on values like
    # Infinity and would reflect arbitrary client data; report only where and why.
    errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path

    # Cookie-authenticated form posts must come from this site (CSRF defence in
    # depth on top of SameSite=Lax). The JSON API uses a header key, not cookies.
    if request.method not in SAFE_METHODS and not path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host", ""):
            return PlainTextResponse("Cross-origin request blocked", status_code=403)

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if is_production():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if not path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(api_entities_router, prefix="/api/v1")
app.include_router(api_runs_router, prefix="/api/v1")
app.include_router(ui_router)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "ui" / "static")), name="static")


@app.get("/health")
def health():
    return {"status": "ok", "service": "profit-first", "version": app.version}


@app.get("/health/db")
def health_db():
    """Also touches the database; point an uptime pinger here so the free web
    service and the free database both stay awake."""
    try:
        with get_db() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - report unhealthy without leaking details
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok", "database": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
