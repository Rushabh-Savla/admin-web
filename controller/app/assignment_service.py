import logging
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from sqlalchemy import select, func, and_, exists
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from controller.app.models import (
    User,
    Dataset,
    DatasetFile,
    Assignment,
    AssignmentItem,
    ActiveImageClaim,
    SyncJob,
    AuditEvent,
)
from controller.app.config import settings

logger = logging.getLogger(__name__)


def create_atomic_assignment(
    db: Session,
    dataset_id: int,
    directory_scope: str,
    scope_type: str,
    annotator_id: int,
    creator_id: int,
    requested_count: int,
    idempotency_key: Optional[str] = None,
    batch_size: Optional[int] = None,
    allow_partial: bool = False,
) -> Assignment:
    """
    Atomically allocate an exact number of eligible images to an annotator.
    Guarantees all-or-nothing: allocates exactly requested_count or rolls back completely.
    Uses PostgreSQL FOR UPDATE SKIP LOCKED and unique primary key on ActiveImageClaim.
    """
    if requested_count <= 0:
        raise ValueError("Requested count must be a positive integer")

    # 1. Check idempotency
    if idempotency_key:
        existing = db.query(Assignment).filter(Assignment.idempotency_key == idempotency_key).first()
        if existing:
            return existing

    # 2. Validate annotator
    annotator = db.query(User).filter(User.id == annotator_id, User.is_active.is_(True)).first()
    if not annotator:
        raise ValueError("Annotator does not exist or is disabled")
    if annotator.role != "ANNOTATOR":
        raise ValueError(f"User {annotator.username} is not an ANNOTATOR")

    # 3. Validate dataset
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        raise ValueError("Dataset does not exist")
    if not dataset.current_catalog_version:
        raise ValueError("Dataset has not been cataloged yet")

    # 4. Scope normalization
    norm_scope = directory_scope.strip("/").strip()

    # 5. Build candidate selection query with lock
    # Exclude files that are currently claimed
    has_active_claim = exists().where(ActiveImageClaim.file_id == DatasetFile.id)
    # Exclude files already verified complete in historical assignments
    is_already_complete = exists().where(
        and_(
            AssignmentItem.file_id == DatasetFile.id,
            AssignmentItem.item_state == "VERIFIED_COMPLETE",
        )
    )

    query = (
        db.query(DatasetFile)
        .filter(
            DatasetFile.dataset_id == dataset_id,
            DatasetFile.category == "IMAGE",
            DatasetFile.file_state == "ELIGIBLE",
            ~has_active_claim,
            ~is_already_complete,
        )
    )

    # Apply folder scope
    if norm_scope:
        if scope_type.upper() == "DIRECT":
            # Exactly direct children in this directory
            # e.g., norm_scope = "folder/sub", file path = "folder/sub/img.jpg"
            prefix = norm_scope + "/"
            query = query.filter(
                DatasetFile.normalized_path.startswith(prefix),
                ~DatasetFile.normalized_path.like(prefix + "%/%"),
            )
        else:
            # Recursive scope
            query = query.filter(
                (DatasetFile.normalized_path == norm_scope) |
                (DatasetFile.normalized_path.startswith(norm_scope + "/"))
            )
    else:
        if scope_type.upper() == "DIRECT":
            # Root direct only (no slashes)
            query = query.filter(~DatasetFile.normalized_path.like("%/%"))

    # Order deterministically
    query = query.order_by(DatasetFile.normalized_path.asc())

    # PostgreSQL FOR UPDATE SKIP LOCKED
    # If running on SQLite (in some unit tests), with_for_update may be a no-op or ignored
    try:
        query = query.with_for_update(skip_locked=True)
    except Exception:
        pass

    # Fetch candidates limited to requested count
    candidates: List[DatasetFile] = query.limit(requested_count).all()

    if not candidates:
        db.rollback()
        raise ValueError(
            f"Cannot fulfill assignment: 0 eligible images available in scope '{norm_scope or '/'}'."
        )

    if not allow_partial and len(candidates) < requested_count:
        db.rollback()
        raise ValueError(
            f"Cannot fulfill assignment: requested {requested_count} images, but only {len(candidates)} eligible images are available in scope '{norm_scope or '/'}'."
        )

    allocated_count = len(candidates)

    # 6. Create Assignment record
    assignment = Assignment(
        dataset_id=dataset_id,
        directory_scope=norm_scope,
        scope_type=scope_type.upper(),
        annotator_id=annotator_id,
        creator_id=creator_id,
        requested_count=requested_count,
        allocated_count=allocated_count,
        batch_size=batch_size or allocated_count,
        status="PREPARING",
        catalog_version=dataset.current_catalog_version,
        lease_generation=1,
        idempotency_key=idempotency_key,
    )
    db.add(assignment)
    db.flush()  # populate assignment.id

    # Automatically set NAS subdirectory under main dataset directory with annotator name
    nas_main_dir = dataset.remote_path.strip("/") if dataset.remote_path else settings.ANNOTATION_WORKSPACE_ROOT
    assignment.workspace_path = f"{nas_main_dir}/{annotator.username}/assignment_{assignment.id}"

    # 7. Create assignment items and active claims
    now_utc = datetime.now(timezone.utc)
    for idx, f in enumerate(candidates, start=1):
        item = AssignmentItem(
            assignment_id=assignment.id,
            file_id=f.id,
            ordinal=idx,
            item_state="PENDING",
        )
        db.add(item)

        claim = ActiveImageClaim(
            file_id=f.id,
            assignment_id=assignment.id,
            annotator_id=annotator_id,
            claim_generation=1,
            claimed_at=now_utc,
        )
        db.add(claim)

    # 8. Enqueue background staging job
    staging_job = SyncJob(
        job_type="PREPARE_ASSIGNMENT",
        status="QUEUED",
        payload={
            "assignment_id": assignment.id,
            "dataset_id": dataset_id,
            "annotator_id": annotator_id,
            "file_count": requested_count,
        },
        idempotency_key=f"prepare_assign_{assignment.id}",
    )
    db.add(staging_job)

    # 9. Audit event
    audit = AuditEvent(
        user_id=creator_id,
        action="ASSIGNMENT_CREATED",
        resource_type="assignment",
        resource_id=str(assignment.id),
        details={
            "dataset_id": dataset_id,
            "annotator_id": annotator_id,
            "requested_count": requested_count,
            "scope": norm_scope,
            "scope_type": scope_type,
        },
    )
    db.add(audit)

    try:
        db.commit()
        db.refresh(assignment)
        return assignment
    except IntegrityError as exc:
        db.rollback()
        raise RuntimeError(f"Concurrent claim conflict detected during reservation: {exc}")


def pause_assignment(db: Session, assignment_id: int, user_id: int) -> Assignment:
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise ValueError("Assignment not found")
    if assignment.status not in {"PREPARING", "READY", "IN_PROGRESS"}:
        raise ValueError(f"Cannot pause assignment in status {assignment.status}")

    assignment.status = "PAUSED"
    audit = AuditEvent(
        user_id=user_id,
        action="ASSIGNMENT_PAUSED",
        resource_type="assignment",
        resource_id=str(assignment.id),
        details={"previous_status": assignment.status},
    )
    db.add(audit)
    db.commit()
    db.refresh(assignment)
    return assignment


def resume_assignment(db: Session, assignment_id: int, user_id: int) -> Assignment:
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise ValueError("Assignment not found")
    if assignment.status != "PAUSED":
        raise ValueError(f"Assignment is not paused (status: {assignment.status})")

    assignment.status = "READY"
    audit = AuditEvent(
        user_id=user_id,
        action="ASSIGNMENT_RESUMED",
        resource_type="assignment",
        resource_id=str(assignment.id),
        details={},
    )
    db.add(audit)
    db.commit()
    db.refresh(assignment)
    return assignment


def cancel_assignment(db: Session, assignment_id: int, user_id: int) -> Assignment:
    """
    Cancel an assignment.
    Releases all active claims for items that have not been completed so images can be reassigned.
    Restores any uncompleted staged images back to the main dataset directory.
    Preserves all historical assignment_items and records!
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise ValueError("Assignment not found")
    if assignment.status == "COMPLETED":
        raise ValueError("Cannot cancel an already completed assignment")

    assignment.status = "CANCELLED"

    # Remove active claims associated with this assignment
    db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment_id).delete()

    # Update unfinished item states to CANCELLED
    db.query(AssignmentItem).filter(
        AssignmentItem.assignment_id == assignment_id,
        AssignmentItem.item_state != "VERIFIED_COMPLETE",
    ).update({"item_state": "CANCELLED"})

    # Restore uncompleted staged images from workspace back to the dataset directory on NAS
    if assignment.workspace_path:
        dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
        rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
        if dataset and rclone_cfg.exists() and rclone_cfg.stat().st_size > 0:
            remote_ws_images = f"{dataset.remote_name}:{assignment.workspace_path}/images"
            all_items = (
                db.query(AssignmentItem, DatasetFile)
                .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
                .filter(AssignmentItem.assignment_id == assignment.id)
                .all()
            )
            has_images_prefix = all(f.normalized_path.startswith("images/") for _, f in all_items) if all_items else False
            clean_remote_path = dataset.remote_path.strip("/")
            restore_dest = f"{dataset.remote_name}:{clean_remote_path}" if has_images_prefix else f"{dataset.remote_name}:{clean_remote_path}/images"
            try:
                cmd = [
                    settings.RCLONE_BIN,
                    "move",
                    remote_ws_images,
                    restore_dest,
                    "--config", settings.RCLONE_CONFIG_PATH,
                    "--transfers", "16",
                    "--checkers", "16",
                    "--timeout", "120s",
                ]
                res = subprocess.run(cmd, capture_output=True, timeout=180)
                if res.returncode == 0:
                    logger.info(f"Restored images for cancelled assignment {assignment_id} to {restore_dest}")
                else:
                    logger.warning(f"Rclone restore note: {res.stderr}")
            except Exception as e:
                logger.warning(f"Failed to restore images on cancel: {e}")

    audit = AuditEvent(
        user_id=user_id,
        action="ASSIGNMENT_CANCELLED",
        resource_type="assignment",
        resource_id=str(assignment.id),
        details={"claims_released": True},
    )
    db.add(audit)
    db.commit()
    db.refresh(assignment)
    return assignment

