import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session

from controller.app.models import Assignment, AssignmentItem, DatasetFile, Dataset
from controller.app.config import settings

logger = logging.getLogger("sync_worker.batch_staging")


class BatchStagingError(Exception):
    """Raised when batch staging or validation fails."""
    pass


def validate_relative_path(path_str: str) -> str:
    """
    Validate that relative path is safe:
    - Not absolute
    - Does not contain parent traversal ('..')
    - Normalized with forward slashes
    """
    if not path_str or not isinstance(path_str, str):
        raise BatchStagingError(f"Invalid path value: {path_str}")

    clean = path_str.replace("\\", "/").strip().lstrip("/")
    if not clean:
        raise BatchStagingError("Empty normalized path")

    pure = PurePosixPath(clean)
    if pure.is_absolute():
        raise BatchStagingError(f"Absolute paths not permitted in batch manifest: {path_str}")

    parts = pure.parts
    if ".." in parts or "." in parts:
        raise BatchStagingError(f"Directory traversal detected in manifest path: {path_str}")

    return clean


def get_assignment_manifest_data(
    db: Session,
    assignment_id: int,
) -> Tuple[Assignment, Dataset, List[Dict[str, Any]]]:
    """
    Retrieve and validate assignment records and build a clean manifest from PostgreSQL.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise BatchStagingError(f"Assignment #{assignment_id} not found in database.")

    if assignment.status not in {"READY", "IN_PROGRESS", "PREPARING"}:
        raise BatchStagingError(
            f"Assignment #{assignment_id} is in status '{assignment.status}', not downloadable."
        )

    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
    if not dataset:
        raise BatchStagingError(f"Dataset #{assignment.dataset_id} not found for assignment #{assignment_id}.")

    # Fetch assigned items with file details
    records = (
        db.query(AssignmentItem, DatasetFile)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(AssignmentItem.assignment_id == assignment_id)
        .order_by(AssignmentItem.ordinal.asc())
        .all()
    )

    if not records:
        raise BatchStagingError(f"No items found for assignment #{assignment_id}.")

    manifest_items = []
    for item, f in records:
        safe_rel_path = validate_relative_path(f.normalized_path)
        manifest_items.append({
            "item_id": item.id,
            "file_id": f.id,
            "ordinal": item.ordinal,
            "relative_path": safe_rel_path,
            "size": f.size,
            "fingerprint": f.fingerprint,
        })

    return assignment, dataset, manifest_items


def stage_batch_download(
    db: Session,
    assignment_id: int,
    mock_base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Stage an entire batch of images in local durable storage as ONE coordinated bulk transfer.

    Guarantees:
    - 0 per-image NAS calls.
    - Exactly 1 bulk rclone copy command per batch (if NAS transfer needed).
    - Local staging cache deduplication.
    - Strict validation of all files before atomic publication.
    - Safe archive (bundle.tar.gz) creation for streaming to desktop clients.
    """
    start_time = time.time()
    assignment, dataset, manifest_items = get_assignment_manifest_data(db, assignment_id)
    total_expected = len(manifest_items)

    staging_root = Path(settings.BATCH_STAGING_DIR).resolve()
    cache_root = Path(settings.CONTROLLER_CACHE_DIR).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    cache_root.mkdir(parents=True, exist_ok=True)

    batch_final_dir = staging_root / f"batch_{assignment_id}"
    batch_partial_dir = staging_root / f"batch_{assignment_id}.partial"
    bundle_final_file = batch_final_dir / "bundle.tar.gz"

    # 1. Idempotency check: If already fully staged, validated, and packaged, return early!
    if bundle_final_file.exists() and (batch_final_dir / "manifest.json").exists():
        final_images_dir = batch_final_dir / "images"
        all_present = True
        for it in manifest_items:
            expected_p = final_images_dir / it["relative_path"]
            if not expected_p.exists() or expected_p.stat().st_size == 0:
                all_present = False
                break
        if all_present:
            logger.info(f"Batch #{assignment_id} is already staged and verified at {batch_final_dir}.")
            return {
                "assignment_id": assignment_id,
                "status": "READY",
                "total_images": total_expected,
                "cached": True,
                "bundle_path": str(bundle_final_file),
                "duration_seconds": round(time.time() - start_time, 3),
                "bulk_commands_invoked": 0,
            }

    # 2. Prepare isolated partial directory
    if batch_partial_dir.exists():
        shutil.rmtree(batch_partial_dir, ignore_errors=True)

    images_partial_dir = batch_partial_dir / "images"
    images_partial_dir.mkdir(parents=True, exist_ok=True)

    # 3. Deduplication: Check local staging cache before querying NAS
    cache_hits = 0
    files_to_transfer: List[Dict[str, Any]] = []

    for it in manifest_items:
        rel_p = it["relative_path"]
        expected_size = it["size"]
        cache_candidate = cache_root / rel_p

        # Check if already cached locally with valid size
        if cache_candidate.exists() and (expected_size <= 0 or cache_candidate.stat().st_size == expected_size):
            dest_p = images_partial_dir / rel_p
            dest_p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache_candidate, dest_p)
            cache_hits += 1
        else:
            files_to_transfer.append(it)

    bulk_commands_invoked = 0
    bytes_transferred = 0

    # 4. If any files remain, perform ONE coordinated bulk transfer
    if files_to_transfer:
        logger.info(
            f"Batch #{assignment_id}: {cache_hits}/{total_expected} images resolved from local cache. "
            f"Transferring remaining {len(files_to_transfer)} images in 1 bulk operation..."
        )

        # Check mode: mock directory vs real Rclone NAS
        rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
        is_mock = mock_base_dir is not None or not rclone_cfg.exists() or rclone_cfg.stat().st_size == 0

        if is_mock:
            # Mock mode: copy from mock storage root in 1 pass
            mock_base = Path(mock_base_dir) if mock_base_dir else Path(os.getenv("MOCK_NAS_DIR", "/tmp/nas_annotation_mock"))
            
            # Determine source root
            if assignment.workspace_path:
                src_root = (mock_base / assignment.workspace_path / "images").resolve()
                if not src_root.exists():
                    src_root = (mock_base / dataset.remote_path.lstrip("/")).resolve()
            else:
                src_root = (mock_base / dataset.remote_path.lstrip("/")).resolve()

            for it in files_to_transfer:
                src_p = src_root / it["relative_path"]
                dest_p = images_partial_dir / it["relative_path"]
                dest_p.parent.mkdir(parents=True, exist_ok=True)
                if not src_p.exists():
                    raise BatchStagingError(f"Mock source file missing on storage: {src_p}")
                shutil.copy2(src_p, dest_p)
                bytes_transferred += dest_p.stat().st_size
            bulk_commands_invoked = 1

        else:
            # Real Rclone NAS mode: Prepare 1 manifest text file and execute 1 rclone copy
            manifest_txt = staging_root / f"manifest_{assignment_id}_{int(time.time())}.txt"
            with open(manifest_txt, "w", encoding="utf-8") as mf:
                for it in files_to_transfer:
                    mf.write(it["relative_path"] + "\n")

            # Determine remote source
            if assignment.workspace_path:
                remote_source = f"{dataset.remote_name}:{assignment.workspace_path}/images"
            else:
                remote_source = f"{dataset.remote_name}:{dataset.remote_path.strip('/')}"

            rclone_cmd = [
                settings.RCLONE_BIN,
                "copy",
                remote_source,
                str(images_partial_dir),
                "--files-from-raw", str(manifest_txt),
                "--transfers", str(settings.RCLONE_BATCH_TRANSFERS),
                "--checkers", str(settings.RCLONE_BATCH_CHECKERS),
                "--bwlimit", str(settings.RCLONE_BATCH_BANDWIDTH),
                "--retries", str(settings.RCLONE_RETRIES),
                "--low-level-retries", str(settings.RCLONE_LOW_LEVEL_RETRIES),
                "--timeout", settings.RCLONE_TIMEOUT,
                "--contimeout", settings.RCLONE_CONNECT_TIMEOUT,
                "--stats", "1m",
                "--stats-one-line",
                "--log-level", "NOTICE",
            ]
            if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                rclone_cmd += ["--config", settings.RCLONE_CONFIG_PATH]

            logger.info(f"Executing coordinated bulk transfer command for batch #{assignment_id}: {' '.join(rclone_cmd)}")
            try:
                proc = subprocess.run(
                    rclone_cmd,
                    capture_output=True,
                    text=True,
                    timeout=settings.TRANSFER_TIMEOUT_SECONDS,
                )
                bulk_commands_invoked += 1
                if proc.returncode != 0:
                    raise BatchStagingError(
                        f"Rclone batch copy failed (code {proc.returncode}): {proc.stderr[:1000]}"
                    )
            finally:
                manifest_txt.unlink(missing_ok=True)

    # 5. Validation of all staged files (Step D)
    logger.info(f"Validating {total_expected} staged files in {images_partial_dir}...")
    verified_items = []
    missing_files = []

    for it in manifest_items:
        rel_p = it["relative_path"]
        fpath = images_partial_dir / rel_p

        # Traversal safety check
        try:
            fpath.resolve().relative_to(images_partial_dir.resolve())
        except ValueError:
            raise BatchStagingError(f"Security traversal detected: file {rel_p} escaped staging directory!")

        if not fpath.exists() or not fpath.is_file():
            missing_files.append(rel_p)
            continue

        actual_size = fpath.stat().st_size
        if actual_size == 0:
            missing_files.append(f"{rel_p} (zero bytes)")
            continue

        verified_items.append({
            **it,
            "actual_size": actual_size,
        })

        # Cache valid file for cross-batch deduplication
        cache_dest = cache_root / rel_p
        if not cache_dest.exists():
            cache_dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(fpath, cache_dest)
            except Exception:
                pass

    if missing_files:
        shutil.rmtree(batch_partial_dir, ignore_errors=True)
        raise BatchStagingError(
            f"Batch #{assignment_id} validation failed: {len(missing_files)}/{total_expected} images missing or truncated! "
            f"Sample missing: {missing_files[:5]}"
        )

    # 6. Write manifest.json inside partial batch
    manifest_doc = {
        "assignment_id": assignment.id,
        "dataset_id": dataset.id,
        "catalog_version": assignment.catalog_version,
        "annotator_id": assignment.annotator_id,
        "total_images": total_expected,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "items": verified_items,
    }
    manifest_file = batch_partial_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_doc, indent=2), encoding="utf-8")

    # 7. Package into bundle.tar.gz for streaming delivery
    bundle_partial = batch_partial_dir / "bundle.tar.gz"
    logger.info(f"Packaging {total_expected} images into compressed bundle {bundle_partial}...")
    with tarfile.open(bundle_partial, "w:gz") as tar:
        # Add manifest
        tar.add(str(manifest_file), arcname="manifest.json")
        # Add images preserving relative path structure
        tar.add(str(images_partial_dir), arcname="images")

    bundle_size = bundle_partial.stat().st_size

    # 8. Atomic transition: Rename partial staging directory to final directory
    if batch_final_dir.exists():
        shutil.rmtree(batch_final_dir, ignore_errors=True)

    batch_partial_dir.rename(batch_final_dir)
    elapsed = time.time() - start_time

    logger.info(
        f"Batch #{assignment_id} successfully staged and published! "
        f"Images: {total_expected}, Bundle size: {bundle_size / (1024*1024):.2f} MB, "
        f"Duration: {elapsed:.2f}s, Bulk commands: {bulk_commands_invoked}"
    )

    return {
        "assignment_id": assignment_id,
        "status": "READY",
        "total_images": total_expected,
        "bundle_size_bytes": bundle_size,
        "bundle_path": str(batch_final_dir / "bundle.tar.gz"),
        "duration_seconds": round(elapsed, 3),
        "cache_hits": cache_hits,
        "transferred_count": len(files_to_transfer),
        "bulk_commands_invoked": bulk_commands_invoked,
    }
