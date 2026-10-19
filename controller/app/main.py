import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from controller.app.config import settings
from controller.app.routers import (
    auth,
    health,
    users,
    datasets,
    assignments,
    submissions,
    sync_jobs,
    images,
    dashboard,
    nas,
)
from controller.app.nas_service import sync_rclone_config_from_env

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("controller")

# Synchronize Rclone configuration from .env if credentials provided
sync_rclone_config_from_env()

app = FastAPI(
    title="NAS Data Annotation Central Platform",
    description="Centralized multi-computer annotation platform with atomic PostgreSQL image ownership and Rclone NAS synchronization.",
    version="2.0.0",
)

# CORS configuration - Allow access from localhost and any LAN / subnet IP address
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for server-rendered admin dashboard (zero external CDN dependencies)
static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include API and dashboard routers
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(datasets.router)
app.include_router(assignments.router)
app.include_router(images.router)
app.include_router(submissions.router)
app.include_router(sync_jobs.router)
app.include_router(nas.router)
app.include_router(dashboard.router)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error processing {request.method} {request.url.path}: {exc}", exc_info=True)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred."},
        )
    raise exc

