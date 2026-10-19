from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import SyncJob, User, Assignment
from controller.app.schemas import SyncJobOut
from controller.app.dependencies import require_role

router = APIRouter(prefix="/api/v1/jobs", tags=["Sync Jobs"])


@router.get("", response_model=List[SyncJobOut])
def list_jobs(
    status_filter: Optional[str] = None,
    job_type: Optional[str] = None,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    query = db.query(SyncJob)
    if status_filter:
        query = query.filter(SyncJob.status == status_filter.upper())
    if job_type:
        query = query.filter(SyncJob.job_type == job_type.upper())
    return query.order_by(SyncJob.id.desc()).limit(100).all()


@router.post("/{job_id}/retry", response_model=SyncJobOut)
def retry_job(
    job_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    job = db.query(SyncJob).filter(SyncJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    job.status = "QUEUED"
    job.attempts = 0
    job.error = None
    job.next_attempt_at = datetime.now(timezone.utc)

    if job.job_type == "PREPARE_ASSIGNMENT":
        assign_id = job.payload.get("assignment_id")
        if assign_id:
            assign = db.query(Assignment).filter(Assignment.id == assign_id).first()
            if assign and assign.status in ["FAILED", "PREPARING"]:
                assign.status = "PREPARING"

    db.commit()
    db.refresh(job)
    return job

