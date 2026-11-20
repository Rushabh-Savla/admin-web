from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from controller.app.database import get_db
from datetime import datetime, timezone
from controller.app.models import (
    Assignment,
    AssignmentItem,
    DatasetFile,
    User,
    ActiveImageClaim,
    AuditEvent,
    BackgroundDeclaration,
)
from controller.app.schemas import (
    AssignmentCreate,
    AssignmentOut,
    AssignmentManifest,
    AssignmentManifestItem,
    AssignmentItemOut,
)
from controller.app.dependencies import get_current_user, require_role
from controller.app.assignment_service import (
    create_atomic_assignment,
    pause_assignment,
    resume_assignment,
    cancel_assignment,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assignments", tags=["Assignments"])


@router.get("", response_model=List[AssignmentOut])
def list_assignments(
    annotator_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Assignment)
    if current_user.role == "ANNOTATOR":
        # Annotator only sees their own assignments
        query = query.filter(Assignment.annotator_id == current_user.id)
    elif annotator_id:
        query = query.filter(Assignment.annotator_id == annotator_id)

    if status_filter:
        query = query.filter(Assignment.status == status_filter.upper())

    return query.order_by(Assignment.id.desc()).all()


@router.post("", response_model=AssignmentOut, status_code=status.HTTP_201_CREATED)
def create_assignment(
    req: AssignmentCreate,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    try:
        assignment = create_atomic_assignment(
            db=db,
            dataset_id=req.dataset_id,
            directory_scope=req.directory_scope,
            scope_type=req.scope_type,
            annotator_id=req.annotator_id,
            creator_id=current_user.id,
            requested_count=req.requested_count,
            idempotency_key=req.idempotency_key,
            batch_size=req.batch_size,
            allow_partial=True,
        )
        return assignment
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/{assignment_id}", response_model=AssignmentOut)
def get_assignment(
    assignment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this assignment")

    return assignment


@router.get("/{assignment_id}/items", response_model=List[AssignmentItemOut])
def get_assignment_items(
    assignment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    items = (
        db.query(AssignmentItem, DatasetFile)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(AssignmentItem.assignment_id == assignment_id)
        .order_by(AssignmentItem.ordinal.asc())
        .all()
    )

    out = []
    for item, f in items:
        out.append(
            AssignmentItemOut(
                id=item.id,
                assignment_id=item.assignment_id,
                file_id=item.file_id,
                ordinal=item.ordinal,
                item_state=item.item_state,
                error=item.error,
                file_path=f.normalized_path,
                file_size=f.size,
            )
        )
    return out


@router.get("/{assignment_id}/manifest", response_model=AssignmentManifest)
def get_assignment_manifest(
    assignment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns authorized manifest items for an assignment.
    Rejects requests if user is not the assigned annotator or manager.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot access another user's assignment")

    items = (
        db.query(AssignmentItem, DatasetFile)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(AssignmentItem.assignment_id == assignment_id)
        .order_by(AssignmentItem.ordinal.asc())
        .all()
    )

    manifest_items = [
        AssignmentManifestItem(
            item_id=item.id,
            file_id=f.id,
            ordinal=item.ordinal,
            relative_path=f.normalized_path,
            size=f.size,
            fingerprint=f.fingerprint,
        )
        for item, f in items
    ]

    return AssignmentManifest(
        assignment_id=assignment.id,
        catalog_version=assignment.catalog_version,
        lease_generation=assignment.lease_generation,
        total_count=len(manifest_items),
        items=manifest_items,
    )


@router.post("/{assignment_id}/pause", response_model=AssignmentOut)
def pause(
    assignment_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    try:
        return pause_assignment(db, assignment_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{assignment_id}/resume", response_model=AssignmentOut)
def resume(
    assignment_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    try:
        return resume_assignment(db, assignment_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{assignment_id}/cancel", response_model=AssignmentOut)
def cancel(
    assignment_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    try:
        return cancel_assignment(db, assignment_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/{assignment_id}/submit-completion")
def submit_completion(
    assignment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot submit another user's assignment")

    if assignment.status == "COMPLETED":
        return {"message": "Assignment is already completed.", "status": assignment.status}

    assignment.status = "SUBMITTED_FOR_REVIEW"
    db.add(
        AuditEvent(
            user_id=current_user.id,
            action="ASSIGNMENT_SUBMITTED_FOR_COMPLETION",
            resource_type="ASSIGNMENT",
            resource_id=assignment.id,
            details=f"Annotator {current_user.username} submitted assignment #{assignment.id} for master completion approval.",
        )
    )
    db.commit()
    db.refresh(assignment)
    return {"message": f"Assignment #{assignment_id} submitted for master completion review.", "status": assignment.status}


@router.post("/{assignment_id}/approve-completion")
def approve_completion(
    assignment_id: int,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    assignment.status = "COMPLETED"

    # Mark all items in the assignment as VERIFIED_COMPLETE (approved as a whole assignment)
    db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).update(
        {AssignmentItem.item_state: "VERIFIED_COMPLETE", AssignmentItem.error: None},
        synchronize_session=False,
    )

    # Approve all background declarations under this assignment
    db.query(BackgroundDeclaration).filter(BackgroundDeclaration.assignment_id == assignment.id).update(
        {
            BackgroundDeclaration.approval_state: "APPROVED",
            BackgroundDeclaration.approved_by: current_user.id,
            BackgroundDeclaration.approved_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )

    # Release active image claims
    file_ids = [
        row.file_id
        for row in db.query(ActiveImageClaim.file_id)
        .filter(ActiveImageClaim.assignment_id == assignment.id)
        .all()
    ]
    claims_deleted = (
        db.query(ActiveImageClaim)
        .filter(ActiveImageClaim.assignment_id == assignment.id)
        .delete(synchronize_session=False)
    )
    if file_ids:
        db.query(DatasetFile).filter(DatasetFile.id.in_(file_ids)).update(
            {DatasetFile.file_state: "ASSIGNED"},
            synchronize_session=False,
        )

    # Consolidate all annotated & non-annotated images + sidecar JSONs back to the main project directory
    try:
        from controller.app.assignment_service import consolidate_assignment_to_main_directory
        consolidate_assignment_to_main_directory(db, assignment)
    except Exception as exc:
        logger.warning(f"Error consolidating assignment {assignment.id} files to main directory: {exc}")

    db.add(
        AuditEvent(
            user_id=current_user.id,
            action="ASSIGNMENT_COMPLETION_APPROVED",
            resource_type="ASSIGNMENT",
            resource_id=assignment.id,
            details=f"Master {current_user.username} approved completion of complete assignment #{assignment.id}. All items verified complete, {claims_deleted} active claims released, and files consolidated to main folder.",
        )
    )
    db.commit()
    db.refresh(assignment)
    return {
        "message": f"Complete assignment #{assignment_id} approved! All {assignment.allocated_count} images & annotations consolidated to main folder.",
        "status": assignment.status,
    }


@router.post("/{assignment_id}/deny-completion")
def deny_completion(
    assignment_id: int,
    reason: Optional[str] = None,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    rework_reason = reason.strip() if reason and reason.strip() else "Supervisor denied completion and requested rework."
    assignment.status = "IN_PROGRESS"

    # Reject background declarations
    db.query(BackgroundDeclaration).filter(BackgroundDeclaration.assignment_id == assignment.id).update(
        {
            BackgroundDeclaration.approval_state: "REJECTED",
            BackgroundDeclaration.approved_by: current_user.id,
        },
        synchronize_session=False,
    )

    # Return background/unverified items back to annotating
    db.query(AssignmentItem).filter(
        AssignmentItem.assignment_id == assignment.id,
        AssignmentItem.item_state.in_(["BACKGROUND_DECLARED", "BACKGROUND_REVIEW"]),
    ).update(
        {AssignmentItem.item_state: "ANNOTATING", AssignmentItem.error: f"Master rework: {rework_reason}"},
        synchronize_session=False,
    )

    db.add(
        AuditEvent(
            user_id=current_user.id,
            action="ASSIGNMENT_COMPLETION_DENIED",
            resource_type="ASSIGNMENT",
            resource_id=assignment.id,
            details=f"Master {current_user.username} denied completion of assignment #{assignment.id}: {rework_reason}",
        )
    )
    db.commit()
    db.refresh(assignment)
    return {
        "message": f"Assignment #{assignment_id} completion denied. Returned to In Progress for annotator rework.",
        "status": assignment.status,
        "reason": rework_reason,
    }



