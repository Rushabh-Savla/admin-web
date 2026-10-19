import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import (
    Assignment,
    AssignmentItem,
    DatasetFile,
    AnnotationSubmission,
    BackgroundDeclaration,
    SyncJob,
    User,
    AuditEvent,
)
from controller.app.schemas import (
    AnnotationSubmissionRequest,
    AnnotationSubmissionOut,
    BulkAnnotationSubmissionRequest,
    BulkAnnotationSubmissionOut,
    BackgroundDeclarationRequest,
    BulkBackgroundDeclarationRequest,
    BackgroundApprovalRequest,
)
from controller.app.dependencies import get_current_user, require_role
from sync_worker.transfer import validate_xanylabeling_schema
from controller.app.config import settings

router = APIRouter(prefix="/api/v1/submissions", tags=["Submissions & Validation"])


@router.post("", response_model=AnnotationSubmissionOut)
def submit_annotation(
    req: AnnotationSubmissionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # 1. Idempotency check
    existing = db.query(AnnotationSubmission).filter(AnnotationSubmission.idempotency_key == req.idempotency_key).first()
    if existing:
        return existing

    # 2. Assignment ownership check
    assignment = db.query(Assignment).filter(Assignment.id == req.assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot submit for another user's assignment")

    if assignment.lease_generation != req.ownership_generation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Stale lease generation {req.ownership_generation}; active generation is {assignment.lease_generation}",
        )

    # 3. Item check
    item = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.file_id == req.file_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image file is not part of this assignment")

    # 4. JSON validation
    valid, err_msg = validate_xanylabeling_schema(req.annotation_json)
    validation_state = "VALID" if valid else "INVALID_SCHEMA"

    raw_json_str = json.dumps(req.annotation_json, indent=2)
    if len(raw_json_str.encode("utf-8")) > settings.MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Annotation payload exceeds maximum allowed size")

    sub = AnnotationSubmission(
        assignment_id=assignment.id,
        file_id=req.file_id,
        annotator_id=current_user.id,
        ownership_generation=req.ownership_generation,
        idempotency_key=req.idempotency_key,
        content_hash=req.content_hash,
        raw_content=raw_json_str,
        validation_state=validation_state,
        error=err_msg,
    )
    db.add(sub)
    db.flush()

    if valid:
        item.item_state = "UPLOAD_PENDING"
        item.error = None
        # Enqueue background upload & remote verify job
        upload_job = SyncJob(
            job_type="UPLOAD_ANNOTATION",
            status="QUEUED",
            payload={"submission_id": sub.id, "assignment_id": assignment.id, "file_id": req.file_id},
            idempotency_key=f"upload_sub_{sub.id}",
        )
        db.add(upload_job)
    else:
        item.item_state = "FAILED"
        item.error = err_msg

    db.commit()
    db.refresh(sub)
    return sub


@router.post("/bulk", response_model=BulkAnnotationSubmissionOut)
def submit_annotations_bulk(
    req: BulkAnnotationSubmissionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == req.assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot submit for another user's assignment")

    if assignment.lease_generation != req.ownership_generation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Stale lease generation {req.ownership_generation}; active generation is {assignment.lease_generation}",
        )

    # Pre-fetch items map
    file_ids = [it.file_id for it in req.items]
    items_map = {
        item.file_id: item
        for item in db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.file_id.in_(file_ids),
        )
        .all()
    }

    submitted_sub_ids = []
    validation_errors = []

    for item_data in req.items:
        fid = item_data.file_id
        item = items_map.get(fid)
        if not item:
            validation_errors.append({"file_id": fid, "error": "Image file is not part of this assignment"})
            continue

        existing = db.query(AnnotationSubmission).filter(AnnotationSubmission.idempotency_key == item_data.idempotency_key).first()
        if existing:
            submitted_sub_ids.append(existing.id)
            continue

        valid, err_msg = validate_xanylabeling_schema(item_data.annotation_json)
        raw_json_str = json.dumps(item_data.annotation_json, indent=2)

        if not valid:
            item.item_state = "FAILED"
            item.error = err_msg
            validation_errors.append({"file_id": fid, "error": err_msg})
            continue

        sub = AnnotationSubmission(
            assignment_id=assignment.id,
            file_id=fid,
            annotator_id=current_user.id,
            ownership_generation=req.ownership_generation,
            idempotency_key=item_data.idempotency_key,
            content_hash=item_data.content_hash,
            raw_content=raw_json_str,
            validation_state="VALID",
            error=None,
        )
        db.add(sub)
        db.flush()

        item.item_state = "UPLOAD_PENDING"
        item.error = None
        submitted_sub_ids.append(sub.id)

    # Enqueue a SINGLE batch sync job to upload all JSON files to NAS in 1 shot!
    if submitted_sub_ids:
        batch_job = SyncJob(
            job_type="BATCH_UPLOAD_ANNOTATIONS",
            status="QUEUED",
            payload={"assignment_id": assignment.id, "submission_ids": submitted_sub_ids},
            idempotency_key=f"batch_sub_{assignment.id}_{submitted_sub_ids[0]}_{len(submitted_sub_ids)}",
        )
        db.add(batch_job)

    db.commit()

    return BulkAnnotationSubmissionOut(
        submitted_count=len(submitted_sub_ids),
        errors_count=len(validation_errors),
        validation_errors=validation_errors,
    )


@router.post("/background")
def declare_background_image(
    req: BackgroundDeclarationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == req.assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot declare for another user's assignment")

    item = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.file_id == req.file_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File is not part of this assignment")

    decl = BackgroundDeclaration(
        assignment_id=assignment.id,
        file_id=req.file_id,
        annotator_id=current_user.id,
        reason=req.reason,
        approval_state="PENDING_ASSIGNMENT_APPROVAL",
    )
    db.add(decl)
    item.item_state = "BACKGROUND_DECLARED"
    item.error = None
    db.commit()
    return {"message": "Background declaration recorded as part of assignment.", "declaration_id": decl.id}


@router.post("/background/bulk")
def declare_bulk_background(
    req: BulkBackgroundDeclarationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    assignment = db.query(Assignment).filter(Assignment.id == req.assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot declare for another user's assignment")

    items = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.file_id.in_(req.file_ids),
        )
        .all()
    )
    declared_count = 0
    for item in items:
        decl = db.query(BackgroundDeclaration).filter(
            BackgroundDeclaration.assignment_id == assignment.id,
            BackgroundDeclaration.file_id == item.file_id,
        ).first()
        if not decl:
            decl = BackgroundDeclaration(
                assignment_id=assignment.id,
                file_id=item.file_id,
                annotator_id=current_user.id,
                reason=req.reason,
                approval_state="PENDING_ASSIGNMENT_APPROVAL",
            )
            db.add(decl)
        item.item_state = "BACKGROUND_DECLARED"
        item.error = None
        declared_count += 1

    unfinished_count = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.item_state.in_(["PENDING", "DOWNLOADING", "ANNOTATING"]),
        )
        .count()
    )
    if unfinished_count == 0 and assignment.status in ["IN_PROGRESS", "READY", "PREPARING"]:
        assignment.status = "SUBMITTED_FOR_REVIEW"

    db.commit()
    return {
        "message": f"Successfully declared {declared_count} background images.",
        "count": declared_count,
        "remaining_unannotated_in_assignment": unfinished_count,
    }


@router.get("/reviews")
def list_review_queue(
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    # Only genuine sync upload failures and conflicts require individual item inspection
    items = (
        db.query(AssignmentItem, DatasetFile, Assignment)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .join(Assignment, AssignmentItem.assignment_id == Assignment.id)
        .filter(AssignmentItem.item_state.in_(["FAILED", "CONFLICT"]))
        .all()
    )

    out = []
    for itm, f, assign in items:
        out.append({
            "item_id": itm.id,
            "assignment_id": assign.id,
            "annotator_id": assign.annotator_id,
            "file_id": f.id,
            "file_path": f.normalized_path,
            "state": itm.item_state,
            "error": itm.error,
        })
    return out


@router.post("/reviews/{item_id}/approve-background")
def approve_background(
    item_id: int,
    req: BackgroundApprovalRequest,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    item = db.query(AssignmentItem).filter(AssignmentItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    decl = (
        db.query(BackgroundDeclaration)
        .filter(
            BackgroundDeclaration.assignment_id == item.assignment_id,
            BackgroundDeclaration.file_id == item.file_id,
        )
        .order_by(BackgroundDeclaration.id.desc())
        .first()
    )

    if req.approved:
        item.item_state = "VERIFIED_COMPLETE"
        item.error = None
        if decl:
            decl.approval_state = "APPROVED"
            decl.approved_by = current_user.id
            from datetime import datetime, timezone
            decl.approved_at = datetime.now(timezone.utc)
    else:
        item.item_state = "ANNOTATING"
        item.error = "Background declaration rejected by manager"
        if decl:
            decl.approval_state = "REJECTED"
            decl.approved_by = current_user.id
            from datetime import datetime, timezone
            decl.approved_at = datetime.now(timezone.utc)

    # Check if assignment is now completed
    uncompleted = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == item.assignment_id,
            AssignmentItem.item_state.in_(["PENDING", "DOWNLOADING", "ANNOTATING"]),
        )
        .count()
    )
    if uncompleted == 0:
        assignment = db.query(Assignment).filter(Assignment.id == item.assignment_id).first()
        if assignment:
            assignment.status = "COMPLETED"
            from controller.app.models import ActiveImageClaim
            db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).delete()

    db.commit()
    return {"message": f"Background decision recorded: approved={req.approved}"}

