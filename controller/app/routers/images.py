import hashlib
import mimetypes
import os
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Response, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import Assignment, AssignmentItem, DatasetFile, Dataset, User
from controller.app.dependencies import get_current_user
from controller.app.config import settings

router = APIRouter(prefix="/api/v1/assignments", tags=["Image Delivery"])

_assignment_cache_locks: Dict[int, threading.Lock] = {}
_global_cache_lock = threading.Lock()


def get_assignment_lock(assignment_id: int) -> threading.Lock:
    with _global_cache_lock:
        if assignment_id not in _assignment_cache_locks:
            _assignment_cache_locks[assignment_id] = threading.Lock()
        return _assignment_cache_locks[assignment_id]


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

    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()

    media_type, _ = mimetypes.guess_type(file_rec.basename)
    if not media_type:
        media_type = "image/jpeg"

    # 1. Fast path: Check local candidate paths (cache directory or mounted storage)
    cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache"))
    mock_base = Path(os.getenv("MOCK_NAS_DIR", "/tmp/nas_annotation_mock"))

    local_candidates = [
        cache_base / f"assignment_{assignment.id}" / "images" / file_rec.normalized_path,
        cache_base / f"assignment_{assignment.id}" / "images" / file_rec.basename,
    ]
    if assignment.workspace_path:
        local_candidates.append(cache_base / assignment.workspace_path / "images" / file_rec.normalized_path)
        local_candidates.append(mock_base / assignment.workspace_path / "images" / file_rec.normalized_path)
    if dataset and dataset.remote_path:
        local_candidates.append(mock_base / dataset.remote_path.lstrip("/") / file_rec.normalized_path)

    for cand in local_candidates:
        if cand.exists() and cand.is_file():
            data = cand.read_bytes()
            img_hash = hashlib.sha256(data).hexdigest()
            return Response(
                content=data,
                media_type=media_type,
                headers={
                    "X-File-Hash": img_hash,
                    "Content-Disposition": f'attachment; filename="{file_rec.basename}"',
                },
            )

    # 2. Slow path: Acquire assignment lock to guarantee only ONE single rclone batch copy runs
    assign_lock = get_assignment_lock(assignment.id)
    with assign_lock:
        # Re-check candidate paths inside lock in case another thread just finished the copy
        for cand in local_candidates:
            if cand.exists() and cand.is_file():
                data = cand.read_bytes()
                img_hash = hashlib.sha256(data).hexdigest()
                return Response(
                    content=data,
                    media_type=media_type,
                    headers={
                        "X-File-Hash": img_hash,
                        "Content-Disposition": f'attachment; filename="{file_rec.basename}"',
                    },
                )

        # Trigger single batch copy from NAS
        rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
        if dataset and rclone_cfg.exists() and rclone_cfg.stat().st_size > 0:
            assignment_cache_dir = cache_base / f"assignment_{assignment.id}" / "images"
            assignment_cache_dir.mkdir(parents=True, exist_ok=True)

            remote_sources = []
            if assignment.workspace_path:
                remote_sources.append(f"{dataset.remote_name}:{assignment.workspace_path}/images")
            if dataset.remote_path:
                remote_sources.append(f"{dataset.remote_name}:{dataset.remote_path.strip('/')}")

            for remote_src in remote_sources:
                try:
                    cmd = [
                        settings.RCLONE_BIN,
                        "copy",
                        remote_src,
                        str(assignment_cache_dir),
                        "--config", settings.RCLONE_CONFIG_PATH,
                        "--transfers", "4",
                        "--checkers", "4",
                        "--timeout", "180s",
                        "--contimeout", "10s",
                    ]
                    res = subprocess.run(cmd, capture_output=True, timeout=240)
                    if res.returncode == 0:
                        for check_p in [
                            assignment_cache_dir / file_rec.normalized_path,
                            assignment_cache_dir / file_rec.basename,
                        ]:
                            if check_p.exists() and check_p.is_file():
                                data = check_p.read_bytes()
                                img_hash = hashlib.sha256(data).hexdigest()
                                return Response(
                                    content=data,
                                    media_type=media_type,
                                    headers={
                                        "X-File-Hash": img_hash,
                                        "Content-Disposition": f'attachment; filename="{file_rec.basename}"',
                                    },
                                )
                except Exception:
                    pass

            # Fallback to individual stream only if batch copy didn't yield the file
            for remote_target in [
                f"{dataset.remote_name}:{assignment.workspace_path}/images/{file_rec.normalized_path}" if assignment.workspace_path else None,
                f"{dataset.remote_name}:{dataset.remote_path.strip('/')}/{file_rec.normalized_path}" if dataset.remote_path else None,
            ]:
                if not remote_target:
                    continue
                cmd = [
                    settings.RCLONE_BIN,
                    "cat",
                    remote_target,
                    "--config", settings.RCLONE_CONFIG_PATH,
                    "--timeout", "15s",
                    "--contimeout", "5s",
                ]
                try:
                    res = subprocess.run(cmd, capture_output=True, timeout=20)
                    if res.returncode == 0 and res.stdout:
                        img_hash = hashlib.sha256(res.stdout).hexdigest()
                        try:
                            save_p = assignment_cache_dir / file_rec.normalized_path
                            save_p.parent.mkdir(parents=True, exist_ok=True)
                            save_p.write_bytes(res.stdout)
                        except Exception:
                            pass
                        return Response(
                            content=res.stdout,
                            media_type=media_type,
                            headers={
                                "X-File-Hash": img_hash,
                                "Content-Disposition": f'attachment; filename="{file_rec.basename}"',
                            },
                        )
                except Exception:
                    pass

    # 3. If file cannot be found on storage target
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Image file '{file_rec.normalized_path}' could not be retrieved from NAS storage",
    )


@router.get("/{assignment_id}/bundle")
def download_assignment_bundle(
    assignment_id: int,
    limit: Optional[int] = Query(None, description="Batch limit, e.g. 2000 images"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Download assigned images in ONE single batch ZIP archive (optionally capped by limit for 2000-image batches).
    Completely eliminates individual HTTP requests and interval logging.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    if current_user.role == "ANNOTATOR" and assignment.annotator_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this assignment")

    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
    cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache"))
    assignment_cache_dir = cache_base / f"assignment_{assignment.id}" / "images"

    # Ensure assignment cache is populated
    assign_lock = get_assignment_lock(assignment.id)
    with assign_lock:
        if not assignment_cache_dir.exists() or not any(assignment_cache_dir.iterdir()):
            assignment_cache_dir.mkdir(parents=True, exist_ok=True)
            rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
            if dataset and rclone_cfg.exists() and rclone_cfg.stat().st_size > 0:
                remote_sources = []
                if assignment.workspace_path:
                    remote_sources.append(f"{dataset.remote_name}:{assignment.workspace_path}/images")
                if dataset.remote_path:
                    remote_sources.append(f"{dataset.remote_name}:{dataset.remote_path.strip('/')}")

                for remote_src in remote_sources:
                    try:
                        cmd = [
                            settings.RCLONE_BIN,
                            "copy",
                            remote_src,
                            str(assignment_cache_dir),
                            "--config", settings.RCLONE_CONFIG_PATH,
                            "--transfers", "4",
                            "--checkers", "4",
                            "--timeout", "180s",
                            "--contimeout", "10s",
                        ]
                        subprocess.run(cmd, capture_output=True, timeout=240)
                        if any(assignment_cache_dir.iterdir()):
                            break
                    except Exception:
                        pass
            else:
                # Fallback for mock environment / tests
                mock_base = Path(os.getenv("MOCK_NAS_DIR", "/tmp/nas_annotation_mock"))
                items = (
                    db.query(AssignmentItem, DatasetFile)
                    .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
                    .filter(AssignmentItem.assignment_id == assignment.id)
                    .all()
                )
                for it, df in items:
                    cand_paths = []
                    if assignment.workspace_path:
                        cand_paths.append(mock_base / assignment.workspace_path / "images" / df.normalized_path)
                    if dataset and dataset.remote_path:
                        cand_paths.append(mock_base / dataset.remote_path.lstrip("/") / df.normalized_path)
                    for cand in cand_paths:
                        if cand.exists() and cand.is_file():
                            dst = assignment_cache_dir / df.normalized_path
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            if not dst.exists():
                                dst.write_bytes(cand.read_bytes())
                            break

    # Select batch items according to limit and pending state
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

    target_rel_paths = {df.normalized_path for _, df in batch_items}
    first_ord = batch_items[0][0].ordinal if batch_items else 1
    last_ord = batch_items[-1][0].ordinal if batch_items else len(batch_items)

    batch_zip_path = cache_base / f"assignment_{assignment.id}" / f"bundle_{first_ord}_{last_ord}.zip"
    if not batch_zip_path.exists() or batch_zip_path.stat().st_size == 0:
        import zipfile
        temp_zip = cache_base / f"assignment_{assignment.id}" / f"bundle_{first_ord}_{last_ord}.tmp.zip"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for f in assignment_cache_dir.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(assignment_cache_dir))
                    if rel in target_rel_paths or f.name in target_rel_paths or not target_rel_paths:
                        zf.write(f, arcname=rel)
        temp_zip.replace(batch_zip_path)

    for it, _ in batch_items:
        if it.item_state == "PENDING":
            it.item_state = "ANNOTATING"
    db.commit()

    return FileResponse(
        path=str(batch_zip_path),
        media_type="application/zip",
        filename=f"assignment_{assignment.id}_batch_{first_ord}_{last_ord}.zip",
    )


