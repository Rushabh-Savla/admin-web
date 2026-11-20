import hashlib
import json
import mimetypes
import os
import zipfile
import threading
from pathlib import Path
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Response, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import Assignment, AssignmentItem, DatasetFile, User
from controller.app.dependencies import get_current_user

router = APIRouter(prefix="/api/v1/assignments", tags=["Image Delivery"])

_assignment_cache_locks: Dict[int, threading.Lock] = {}
_global_cache_lock = threading.Lock()


def get_assignment_lock(assignment_id: int) -> threading.Lock:
    with _global_cache_lock:
        if assignment_id not in _assignment_cache_locks:
            _assignment_cache_locks[assignment_id] = threading.Lock()
        return _assignment_cache_locks[assignment_id]


def get_assignment_cache_path(assignment_id: int, relative_path: str) -> Path:
    cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache"))
    cache_root = (cache_base / f"assignment_{assignment_id}" / "images").resolve()
    cache_path = (cache_root / relative_path).resolve()

    try:
        cache_path.relative_to(cache_root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image cache path",
        )

    return cache_path


@router.get("/{assignment_id}/images/{file_id}")
def download_assigned_image(
    assignment_id: int,
    file_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Download image file bytes.
    Security guarantee:
    1. Annotator can ONLY access their own assigned images.
    2. File must belong to the specified assignment.
    3. Traversal protection enforced.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this assignment's files")

    item = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment_id,
            AssignmentItem.file_id == file_id,
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File is not part of this assignment")

    file_rec = db.query(DatasetFile).filter(DatasetFile.id == file_id).first()
    if not file_rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File record missing")

    media_type, _ = mimetypes.guess_type(file_rec.basename)
    if not media_type:
        media_type = "image/jpeg"

    cache_paths = [
        get_assignment_cache_path(assignment.id, file_rec.normalized_path),
        get_assignment_cache_path(assignment.id, file_rec.basename),
    ]

    for cache_path in cache_paths:
        if cache_path.is_file():
            data = cache_path.read_bytes()
            img_hash = hashlib.sha256(data).hexdigest()
            return Response(
                content=data,
                media_type=media_type,
                headers={
                    "X-File-Hash": img_hash,
                    "Content-Disposition": f'attachment; filename="{file_rec.basename}"',
                },
            )

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Assignment cache is not ready",
    )


@router.get("/{assignment_id}/bundle")
def download_assignment_bundle(
    assignment_id: int,
    limit: Optional[int] = Query(None, description="Batch limit, e.g. 2000 images"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a batch ZIP entirely from the prepared assignment cache.
    This endpoint does not initiate any NAS or Rclone operation.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this assignment")

    cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache")).resolve()
    assignment_cache_root = (cache_base / f"assignment_{assignment.id}").resolve()
    assignment_cache_dir = (assignment_cache_root / "images").resolve()

    manifest_paths = [
        assignment_cache_root / "manifest.json",
        assignment_cache_dir / "manifest.json",
    ]
    manifest_path = next((path for path in manifest_paths if path.is_file()), None)

    if not assignment_cache_dir.is_dir() or manifest_path is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assignment cache is not ready",
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_items = manifest.get("items")
        if not isinstance(manifest_items, list):
            raise ValueError("manifest items are missing")
    except (json.JSONDecodeError, OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assignment cache manifest is missing or invalid",
        )

    manifest_by_item_id = {
        item.get("item_id"): item
        for item in manifest_items
        if isinstance(item, dict) and item.get("item_id") is not None
    }

    items_query = (
        db.query(AssignmentItem, DatasetFile)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.item_state.in_(["PENDING", "DOWNLOADING", "ANNOTATING"]),
        )
        .order_by(AssignmentItem.ordinal.asc())
    )
    if limit is not None and limit > 0:
        items_query = items_query.limit(limit)
    batch_items = items_query.all()

    if not batch_items:
        batch_items = (
            db.query(AssignmentItem, DatasetFile)
            .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
            .filter(AssignmentItem.assignment_id == assignment.id)
            .order_by(AssignmentItem.ordinal.asc())
            .all()
        )

    batch_entries = []
    for item, file_rec in batch_items:
        manifest_item = manifest_by_item_id.get(item.id)
        relative_path = None
        if manifest_item:
            relative_path = manifest_item.get("relative_path")
        if not relative_path:
            relative_path = file_rec.normalized_path

        cache_path = get_assignment_cache_path(assignment.id, relative_path)
        if not cache_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Assignment cache is not ready: a requested image is missing",
            )
        batch_entries.append((item, relative_path, cache_path))

    if not batch_entries:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Assignment cache is not ready: assignment manifest is empty",
        )

    first_ord = batch_entries[0][0].ordinal
    last_ord = batch_entries[-1][0].ordinal
    batch_zip_path = assignment_cache_root / f"bundle_{first_ord}_{last_ord}.zip"

    assign_lock = get_assignment_lock(assignment.id)
    with assign_lock:
        if not batch_zip_path.is_file() or batch_zip_path.stat().st_size == 0:
            temp_zip = assignment_cache_root / f"bundle_{first_ord}_{last_ord}.tmp.zip"
            try:
                with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
                    for _, relative_path, cache_path in batch_entries:
                        zf.write(cache_path, arcname=relative_path)
                temp_zip.replace(batch_zip_path)
            finally:
                if temp_zip.exists():
                    temp_zip.unlink()

        for item, _, _ in batch_entries:
            if item.item_state == "PENDING":
                item.item_state = "ANNOTATING"
        db.commit()

    return FileResponse(
        path=str(batch_zip_path),
        media_type="application/zip",
        filename=f"assignment_{assignment.id}_batch_{first_ord}_{last_ord}.zip",
    )


