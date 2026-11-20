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


def release_assignment_claims(db: Session, assignment_id: int) -> int:
    """Release active claims and incrementally update DatasetFile.file_state."""
    file_ids = [
        row.file_id
        for row in db.query(ActiveImageClaim.file_id)
        .filter(ActiveImageClaim.assignment_id == assignment_id)
        .all()
    ]
    deleted = (
        db.query(ActiveImageClaim)
        .filter(ActiveImageClaim.assignment_id == assignment_id)
        .delete(synchronize_session=False)
    )
    if file_ids:
        db.query(DatasetFile).filter(DatasetFile.id.in_(file_ids)).update(
            {DatasetFile.file_state: "ELIGIBLE"},
            synchronize_session=False,
        )
    return deleted


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
        f.file_state = "ASSIGNED"

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


def restore_assignment_workspace(db: Session, assignment: Assignment) -> bool:
    """
    Restore all staged images from an assignment workspace back to the dataset's original root directory.
    Purges the assignment workspace directory and manifest on the NAS, cleans up empty annotator directories,
    and removes any local server controller caches.
    """
    if not assignment or not assignment.workspace_path:
        return True

    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
    if not dataset:
        return True

    clean_remote_path = dataset.remote_path.strip("/")
    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    config_path = str(rclone_cfg)
    try:
        from controller.app.nas_service import resolve_rclone_config_path
        config_path = resolve_rclone_config_path()
    except Exception:
        pass

    import os
    import shutil
    is_real_rclone = dataset.remote_name and os.path.exists(config_path) and os.path.getsize(config_path) > 0

    # Check if files in this assignment originally had an 'images/' prefix in normalized_path
    sample_file = (
        db.query(DatasetFile)
        .join(AssignmentItem, AssignmentItem.file_id == DatasetFile.id)
        .filter(AssignmentItem.assignment_id == assignment.id)
        .first()
    )
    has_images_prefix = bool(sample_file and sample_file.normalized_path.startswith("images/"))
    target_rel_path = f"{clean_remote_path}/images" if has_images_prefix else clean_remote_path

    if is_real_rclone:
        remote_ws_images = f"{dataset.remote_name}:{assignment.workspace_path}/images"
        # Use _restore remote section to prevent Rclone's overlapping remotes restriction
        restore_dest = f"{dataset.remote_name}_restore:{target_rel_path}"

        # 1. Move all staged images back to the dataset's original directory structure
        try:
            cmd = [
                settings.RCLONE_BIN,
                "move",
                remote_ws_images,
                restore_dest,
                "--config", config_path,
                "--transfers", "4",
                "--checkers", "4",
                "--timeout", "180s",
                "--contimeout", "10s",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
            if res.returncode == 0:
                logger.info(f"Successfully consolidated/restored files for assignment {assignment.id} from {remote_ws_images} to {restore_dest}")
            else:
                err_str = (res.stderr or "").lower()
                if "directory not found" in err_str or "not found" in err_str or "doesn't exist" in err_str:
                    logger.info(f"Source workspace images folder {remote_ws_images} does not exist or was already consolidated.")
                else:
                    logger.warning(f"Rclone restore note for assignment {assignment.id} (code {res.returncode}): {res.stderr[:300]}")
        except Exception as exc:
            logger.warning(f"Exception restoring images for assignment {assignment.id} (continuing cleanup): {exc}")

        # 2. Purge the assignment workspace directory (cleans up manifest.json, empty dirs)
        try:
            remote_ws_dir = f"{dataset.remote_name}:{assignment.workspace_path}"
            purge_cmd = [
                settings.RCLONE_BIN,
                "purge",
                remote_ws_dir,
                "--config", config_path,
                "--timeout", "60s",
                "--contimeout", "10s",
            ]
            subprocess.run(purge_cmd, capture_output=True, timeout=90)
        except Exception as exc:
            logger.warning(f"Failed to purge assignment workspace {assignment.workspace_path} (continuing): {exc}")

        # 3. Clean up empty annotator folder if empty (remove without --leave-root so the annotator directory itself is deleted)
        try:
            annotator_parent = str(Path(assignment.workspace_path).parent.as_posix())
            if annotator_parent and annotator_parent != clean_remote_path:
                rmdirs_cmd = [
                    settings.RCLONE_BIN,
                    "rmdirs",
                    f"{dataset.remote_name}:{annotator_parent}",
                    "--config", config_path,
                    "--timeout", "30s",
                ]
                subprocess.run(rmdirs_cmd, capture_output=True, timeout=45)
        except Exception:
            pass

        # 4. Sweep dataset directory for any remaining empty directories while leaving the dataset root intact
        try:
            sweep_cmd = [
                settings.RCLONE_BIN,
                "rmdirs",
                f"{dataset.remote_name}:{clean_remote_path}",
                "--leave-root",
                "--config", config_path,
                "--timeout", "30s",
            ]
            subprocess.run(sweep_cmd, capture_output=True, timeout=45)
        except Exception:
            pass

    else:
        # Mock mode (/tmp/nas_annotation_mock)
        try:
            mock_base = Path(os.getenv("MOCK_NAS_DIR", "/tmp/nas_annotation_mock"))
            ws_images = mock_base / assignment.workspace_path / "images"
            dest_root = mock_base / target_rel_path
            if ws_images.exists():
                for f in ws_images.rglob("*"):
                    if f.is_file():
                        rel = f.relative_to(ws_images)
                        target = dest_root / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(f), str(target))
            shutil.rmtree(mock_base / assignment.workspace_path, ignore_errors=True)
            # Remove empty annotator parent directory if no other assignments remain
            annotator_parent_dir = (mock_base / assignment.workspace_path).parent
            if annotator_parent_dir.exists() and not any(annotator_parent_dir.iterdir()):
                annotator_parent_dir.rmdir()
        except Exception as e:
            logger.warning(f"Mock mode workspace restore note: {e}")

    # 4. Clean up any local server cache.
    try:
        cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache"))
        cache_dir = cache_base / f"assignment_{assignment.id}"
        shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:
        pass

    # Restore assigned claims to eligible incrementally; no full catalog scan is needed.
    release_assignment_claims(db, assignment.id)

    return True


def consolidate_assignment_to_main_directory(db: Session, assignment: Assignment) -> bool:
    """
    Consolidate all annotated and non-annotated images along with sidecar JSON files
    from an assignment workspace back into the main project directory on NAS.
    Purges the assignment workspace and cleans up empty annotator folders.
    """
    return restore_assignment_workspace(db, assignment)


def consolidate_dataset_completed_assignments(db: Session, dataset_id: int) -> dict:
    """
    Consolidate all completed assignments for a project back into its main directory.
    Sweeps any remaining assignment subdirectories on storage and updates catalog.
    """
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        return {"consolidated": 0, "status": "Dataset not found"}

    completed_assignments = (
        db.query(Assignment)
        .filter(
            Assignment.dataset_id == dataset_id,
            Assignment.status == "COMPLETED",
        )
        .all()
    )

    consolidated_count = 0
    for a in completed_assignments:
        try:
            consolidate_assignment_to_main_directory(db, a)
            consolidated_count += 1
        except Exception as e:
            logger.warning(f"Error consolidating assignment {a.id}: {e}")

    # Clean up empty parent directories on storage
    clean_remote_path = dataset.remote_path.strip("/")
    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    config_path = str(rclone_cfg)
    try:
        from controller.app.nas_service import resolve_rclone_config_path
        config_path = resolve_rclone_config_path()
        subprocess.run([
            settings.RCLONE_BIN, "rmdirs", f"{dataset.remote_name}:{clean_remote_path}",
            "--leave-root", "--config", config_path, "--timeout", "30s"
        ], capture_output=True, timeout=45)
    except Exception:
        pass

    db.commit()
    return {"consolidated": consolidated_count, "dataset_id": dataset_id, "status": "success"}


def restore_and_cleanup_dataset(db: Session, dataset_id: int):
    """
    Called before a dataset (project) is deleted from the system.
    Restores all staged images across all assignments back to the dataset's original root directory,
    and removes all annotator assignment subdirectories from the storage.
    Also sweeps any stranded assignment subdirectories on the NAS for this dataset.
    Completely wrapped in safe try-except blocks so manual folder deletions or NAS errors never block project deletion.
    """
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        return

    import json
    import os
    clean_remote_path = dataset.remote_path.strip("/")
    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    config_path = str(rclone_cfg)
    try:
        from controller.app.nas_service import resolve_rclone_config_path
        config_path = resolve_rclone_config_path()
    except Exception:
        pass

    try:
        assignments = db.query(Assignment).filter(Assignment.dataset_id == dataset_id).all()
        for a in assignments:
            try:
                restore_assignment_workspace(db, a)
            except Exception as e:
                logger.warning(f"Error restoring assignment {a.id}: {e}")
    except Exception as e:
        logger.warning(f"Error querying assignments for dataset {dataset_id}: {e}")

    # General sweep: check if any stranded assignment subdirectories exist on NAS under this dataset path
    is_real_rclone = dataset.remote_name and os.path.exists(config_path) and os.path.getsize(config_path) > 0
    if is_real_rclone:
        try:
            list_cmd = [
                settings.RCLONE_BIN,
                "lsjson",
                f"{dataset.remote_name}:{clean_remote_path}",
                "-R",
                "--dirs-only",
                "--config", config_path,
                "--timeout", "30s",
            ]
            l_proc = subprocess.run(list_cmd, capture_output=True, text=True, timeout=45)
            if l_proc.returncode == 0:
                dir_items = json.loads(l_proc.stdout or "[]")
                assignment_dirs = [d["Path"] for d in dir_items if "/assignment_" in d["Path"] or d["Path"].startswith("assignment_")]
                for ad in assignment_dirs:
                    if ad.endswith("/images") or ad == "images":
                        continue
                    try:
                        ws_img_remote = f"{dataset.remote_name}:{clean_remote_path}/{ad}/images"
                        restore_dest = f"{dataset.remote_name}_restore:{clean_remote_path}"
                        subprocess.run([
                            settings.RCLONE_BIN, "move", ws_img_remote, restore_dest,
                            "--config", config_path, "--transfers", "4", "--checkers", "4", "--timeout", "120s"
                        ], capture_output=True, timeout=180)
                    except Exception:
                        pass

                    try:
                        subprocess.run([
                            settings.RCLONE_BIN, "purge", f"{dataset.remote_name}:{clean_remote_path}/{ad}",
                            "--config", config_path, "--timeout", "60s"
                        ], capture_output=True, timeout=90)
                    except Exception:
                        pass

                # Clean up empty parent directories
                try:
                    subprocess.run([
                        settings.RCLONE_BIN, "rmdirs", f"{dataset.remote_name}:{clean_remote_path}",
                        "--leave-root", "--config", config_path, "--timeout", "30s"
                    ], capture_output=True, timeout=45)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Error sweeping stranded assignment folders on NAS: {e}")


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
    try:
        restore_assignment_workspace(db, assignment)
    except Exception as e:
        logger.warning(f"Error restoring workspace on cancel for assignment {assignment_id}: {e}")

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

