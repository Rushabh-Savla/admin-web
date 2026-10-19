from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import exists, and_, or_, func
from controller.app.database import get_db
from controller.app.models import (
    Dataset,
    DatasetDirectory,
    DatasetFile,
    Assignment,
    AssignmentItem,
    ActiveImageClaim,
    AnnotationSubmission,
    BackgroundDeclaration,
    SyncJob,
    User,
    AuditEvent,
)
from controller.app.schemas import DatasetCreate, DatasetOut, DatasetDirectoryOut
from controller.app.dependencies import require_role
from controller.app.assignment_service import restore_and_cleanup_dataset

router = APIRouter(prefix="/api/v1/datasets", tags=["Datasets & Catalog"])


@router.get("", response_model=List[DatasetOut])
def list_datasets(
    current_user: User = Depends(require_role(["ADMIN", "MANAGER", "ANNOTATOR"])),
    db: Session = Depends(get_db),
):
    return db.query(Dataset).order_by(Dataset.id.asc()).all()


@router.post("", response_model=DatasetOut, status_code=status.HTTP_201_CREATED)
def register_dataset(
    req: DatasetCreate,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    existing = db.query(Dataset).filter(Dataset.name == req.name).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dataset name already exists")

    ds = Dataset(
        name=req.name,
        remote_name=req.remote_name,
        remote_path=req.remote_path,
        catalog_state="IDLE",
    )
    db.add(ds)
    db.commit()
    db.refresh(ds)

    audit = AuditEvent(
        user_id=current_user.id,
        action="DATASET_REGISTERED",
        resource_type="dataset",
        resource_id=str(ds.id),
        details={"name": ds.name, "remote_path": ds.remote_path},
    )
    db.add(audit)
    db.commit()
    return ds


@router.get("/{dataset_id}", response_model=DatasetOut)
def get_dataset(
    dataset_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER", "ANNOTATOR"])),
    db: Session = Depends(get_db),
):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")
    return ds


@router.delete("/{dataset_id}")
def delete_dataset(
    dataset_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")

    title = ds.title or ds.name
    # 1. Restore all staged images from annotator workspaces back to the original dataset directory structure on NAS
    try:
        restore_and_cleanup_dataset(db, dataset_id)
    except Exception as r_err:
        pass

    # 2. Cascade delete all related assignments, submissions, claims, items, files, directories, and sync jobs
    assignments = db.query(Assignment).filter(Assignment.dataset_id == dataset_id).all()
    assignment_ids = [a.id for a in assignments]
    files = db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset_id).all()
    file_ids = [f.id for f in files]

    if assignment_ids or file_ids:
        conds_sub = []
        conds_bg = []
        conds_claim = []
        if assignment_ids:
            conds_sub.append(AnnotationSubmission.assignment_id.in_(assignment_ids))
            conds_bg.append(BackgroundDeclaration.assignment_id.in_(assignment_ids))
            conds_claim.append(ActiveImageClaim.assignment_id.in_(assignment_ids))
        if file_ids:
            conds_sub.append(AnnotationSubmission.file_id.in_(file_ids))
            conds_bg.append(BackgroundDeclaration.file_id.in_(file_ids))
            conds_claim.append(ActiveImageClaim.file_id.in_(file_ids))

        if conds_sub:
            db.query(AnnotationSubmission).filter(or_(*conds_sub)).delete(synchronize_session=False)
        if conds_bg:
            db.query(BackgroundDeclaration).filter(or_(*conds_bg)).delete(synchronize_session=False)
        if conds_claim:
            db.query(ActiveImageClaim).filter(or_(*conds_claim)).delete(synchronize_session=False)

    if assignment_ids:
        db.query(AssignmentItem).filter(AssignmentItem.assignment_id.in_(assignment_ids)).delete(synchronize_session=False)
        db.query(Assignment).filter(Assignment.id.in_(assignment_ids)).delete(synchronize_session=False)

    db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset_id).delete(synchronize_session=False)
    db.query(DatasetDirectory).filter(DatasetDirectory.dataset_id == dataset_id).delete(synchronize_session=False)

    all_jobs = db.query(SyncJob).all()
    for j in all_jobs:
        if isinstance(j.payload, dict) and j.payload.get("dataset_id") == dataset_id:
            db.delete(j)

    try:
        db.delete(ds)
    except Exception:
        db.query(Dataset).filter(Dataset.id == dataset_id).delete(synchronize_session=False)

    try:
        audit = AuditEvent(
            user_id=current_user.id,
            action="PROJECT_DELETED",
            resource_type="dataset",
            resource_id=str(dataset_id),
            details={"title": title, "name": getattr(ds, "name", "dataset")},
        )
        db.add(audit)
    except Exception:
        pass

    db.commit()
    return {"message": f"Project '{title}' and all associated assignments deleted successfully"}


@router.post("/{dataset_id}/scan")
def trigger_catalog_scan(
    dataset_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")

    job = SyncJob(
        job_type="CATALOG_SCAN",
        status="QUEUED",
        payload={"dataset_id": ds.id},
    )
    db.add(job)
    ds.catalog_state = "SCANNING"
    db.commit()
    return {"message": "Catalog scan enqueued", "job_id": job.id}


@router.get("/{dataset_id}/directories", response_model=List[DatasetDirectoryOut])
def list_dataset_directories(
    dataset_id: int,
    parent_path: Optional[str] = None,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER", "ANNOTATOR"])),
    db: Session = Depends(get_db),
):
    query = db.query(DatasetDirectory).filter(DatasetDirectory.dataset_id == dataset_id)
    if parent_path is not None:
        query = query.filter(DatasetDirectory.parent_path == parent_path)
    return query.order_by(DatasetDirectory.normalized_path.asc()).all()


@router.get("/{dataset_id}/eligible-count")
def get_eligible_count(
    dataset_id: int,
    scope: str = "",
    scope_type: str = "RECURSIVE",
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    norm_scope = scope.strip("/").strip()
    has_active_claim = exists().where(ActiveImageClaim.file_id == DatasetFile.id)
    is_already_complete = exists().where(
        and_(
            AssignmentItem.file_id == DatasetFile.id,
            AssignmentItem.item_state == "VERIFIED_COMPLETE",
        )
    )

    query = db.query(func.count(DatasetFile.id)).filter(
        DatasetFile.dataset_id == dataset_id,
        DatasetFile.category == "IMAGE",
        DatasetFile.file_state == "ELIGIBLE",
        ~has_active_claim,
        ~is_already_complete,
    )

    if norm_scope:
        if scope_type.upper() == "DIRECT":
            prefix = norm_scope + "/"
            query = query.filter(
                DatasetFile.normalized_path.startswith(prefix),
                ~DatasetFile.normalized_path.like(prefix + "%/%"),
            )
        else:
            query = query.filter(
                (DatasetFile.normalized_path == norm_scope) |
                (DatasetFile.normalized_path.startswith(norm_scope + "/"))
            )
    else:
        if scope_type.upper() == "DIRECT":
            query = query.filter(~DatasetFile.normalized_path.like("%/%"))

    available_count = query.scalar() or 0
    return {"dataset_id": dataset_id, "scope": norm_scope, "scope_type": scope_type, "eligible_count": available_count}


@router.post("/{dataset_id}/consolidate")
def consolidate_dataset_workspaces(
    dataset_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dataset not found")

    from controller.app.assignment_service import consolidate_dataset_completed_assignments
    res = consolidate_dataset_completed_assignments(db, dataset_id)
    return res

