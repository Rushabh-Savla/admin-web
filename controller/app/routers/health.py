import shutil
from fastapi import APIRouter, Depends, status, Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from controller.app.database import get_db
from controller.app.schemas import HealthStatus, ReadinessStatus
from controller.app.config import settings

router = APIRouter(prefix="/api/v1/health", tags=["Health & Readiness"])


@router.get("/liveness", response_model=HealthStatus)
def liveness():
    """Liveness probe: verifies web server process is responsive."""
    return HealthStatus(status="ok", version="2.0.0")


@router.get("/readiness", response_model=ReadinessStatus)
def readiness(response: Response, db: Session = Depends(get_db)):
    """Readiness probe: checks database and essential background dependencies without exposing secrets."""
    db_status = "ok"
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_status = "error"

    rclone_status = "available" if shutil.which(settings.RCLONE_BIN) else "missing"
    worker_queue_status = "ok" if db_status == "ok" else "error"

    overall_status = "ok" if db_status == "ok" else "error"
    if overall_status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessStatus(
        status=overall_status,
        database=db_status,
        worker_queue=worker_queue_status,
        rclone=rclone_status,
    )

