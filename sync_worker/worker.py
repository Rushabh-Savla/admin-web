import logging
import os
import sys
import time
import socket
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from controller.app.database import SessionLocal, engine
from controller.app.models import SyncJob, Dataset, DatasetDirectory, DatasetFile, ActiveImageClaim, Assignment
from controller.app.config import settings
from sync_worker.catalog_adapter import (
    RcloneCatalogAdapter,
    LocalMockCatalogAdapter,
    build_directory_tree_counts,
    calculate_catalog_version,
)
from sync_worker.staging import prepare_assignment_workspace
from sync_worker.transfer import upload_and_verify_annotation, upload_and_verify_annotations_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sync_worker")


def get_worker_name() -> str:
    hostname = socket.gethostname()
    pid = os.getpid()
    return f"worker_{hostname}_{pid}"


def claim_next_job(db: Session, worker_name: str) -> Optional[SyncJob]:
    """
    Atomic job claiming from PostgreSQL using FOR UPDATE SKIP LOCKED.
    """
    now_utc = datetime.now(timezone.utc)
    query = (
        db.query(SyncJob)
        .filter(
            SyncJob.status.in_(["QUEUED", "RETRYING"]),
            SyncJob.next_attempt_at <= now_utc,
        )
        .order_by(SyncJob.id.asc())
    )

    try:
        query = query.with_for_update(skip_locked=True)
    except Exception:
        pass

    job = query.first()
    if not job:
        return None

    job.status = "CLAIMED"
    job.claimed_worker = worker_name
    job.claimed_at = now_utc
    db.commit()
    db.refresh(job)
    return job


def process_catalog_scan(db: Session, job: SyncJob, mock_base_dir: Optional[str] = None):
    dataset_id = job.payload.get("dataset_id")
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not dataset:
        raise ValueError(f"Dataset {dataset_id} not found")

    dataset.catalog_state = "SCANNING"
    db.commit()

    # Determine adapter: mock vs real rclone
    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    is_mock = mock_base_dir is not None or not rclone_cfg.exists() or rclone_cfg.stat().st_size == 0
    if is_mock:
        base = Path(mock_base_dir) if mock_base_dir else Path("/tmp/nas_annotation_mock")
        adapter = LocalMockCatalogAdapter(base)
    else:
        adapter = RcloneCatalogAdapter(
            rclone_bin=settings.RCLONE_BIN,
            config_path=settings.RCLONE_CONFIG_PATH,
        )

    entries = list(
        adapter.scan_files(
            remote_name=dataset.remote_name,
            remote_path=dataset.remote_path,
            image_extensions=settings.SUPPORTED_IMAGE_EXTENSIONS,
            exclusions=settings.EXCLUDED_PATTERNS,
        )
    )

    version = calculate_catalog_version(entries)
    dir_counts_map = build_directory_tree_counts(entries)

    # In one transaction: update directories and files
    now_utc = datetime.now(timezone.utc)

    # Remove previous directory counts
    db.query(DatasetDirectory).filter(DatasetDirectory.dataset_id == dataset.id).delete()

    for path, dc in dir_counts_map.items():
        dir_obj = DatasetDirectory(
            dataset_id=dataset.id,
            normalized_path=path,
            parent_path=dc.parent_path,
            direct_image_count=dc.direct_image_count,
            direct_json_count=dc.direct_json_count,
            direct_txt_count=dc.direct_txt_count,
            direct_other_count=dc.direct_other_count,
            recursive_image_count=dc.recursive_image_count,
            recursive_json_count=dc.recursive_json_count,
            recursive_txt_count=dc.recursive_txt_count,
            recursive_other_count=dc.recursive_other_count,
            updated_at=now_utc,
        )
        db.add(dir_obj)

    # Insert or update files
    # Track existing files to detect changes
    existing_files = {
        f.normalized_path: f
        for f in db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset.id).all()
    }

    claimed_file_ids = {
        c[0] for c in db.query(ActiveImageClaim.file_id).all()
    }

    seen_paths = set()
    for entry in entries:
        seen_paths.add(entry.normalized_path)
        if entry.normalized_path in existing_files:
            file_rec = existing_files[entry.normalized_path]
            file_rec.size = entry.size
            file_rec.remote_mtime = entry.remote_mtime
            file_rec.fingerprint = entry.fingerprint
            file_rec.catalog_version = version
            file_rec.updated_at = now_utc
            if file_rec.id in claimed_file_ids:
                file_rec.file_state = "ASSIGNED"
            elif file_rec.file_state in ["ASSIGNED", "MISSING"]:
                file_rec.file_state = "ELIGIBLE"
        else:
            file_rec = DatasetFile(
                dataset_id=dataset.id,
                normalized_path=entry.normalized_path,
                basename=entry.basename,
                extension=entry.extension,
                category=entry.category,
                size=entry.size,
                remote_mtime=entry.remote_mtime,
                fingerprint=entry.fingerprint,
                catalog_version=version,
                file_state="ELIGIBLE",
                created_at=now_utc,
                updated_at=now_utc,
            )
            db.add(file_rec)

    # For files no longer present in root catalog scan:
    # Check if they are claimed/assigned in an active assignment
    for path, f in existing_files.items():
        if path not in seen_paths:
            if f.id in claimed_file_ids:
                if f.file_state != "ASSIGNED":
                    f.file_state = "ASSIGNED"
                    f.updated_at = now_utc
            elif f.file_state != "MISSING":
                f.file_state = "MISSING"
                f.updated_at = now_utc

    dataset.catalog_state = "READY"
    dataset.current_catalog_version = version
    dataset.updated_at = now_utc
    db.commit()
    logger.info(f"Catalog scan complete for dataset '{dataset.name}'. Total files: {len(entries)}, version: {version}")


def execute_job(db: Session, job: SyncJob, mock_base_dir: Optional[str] = None):
    logger.info(f"Executing job {job.id} (type: {job.job_type}, attempt: {job.attempts + 1})")
    try:
        if job.job_type == "CATALOG_SCAN":
            process_catalog_scan(db, job, mock_base_dir=mock_base_dir)
        elif job.job_type == "PREPARE_ASSIGNMENT":
            p = Path(mock_base_dir) if mock_base_dir else None
            prepare_assignment_workspace(db, job.payload["assignment_id"], mock_base_dir=p)
        elif job.job_type == "BATCH_UPLOAD_ANNOTATIONS":
            p = Path(mock_base_dir) if mock_base_dir else None
            upload_and_verify_annotations_batch(db, job.payload.get("submission_ids", []), mock_base_dir=p)
        elif job.job_type == "UPLOAD_ANNOTATION":
            p = Path(mock_base_dir) if mock_base_dir else None
            assign_id = job.payload.get("assignment_id")
            sub_id = job.payload.get("submission_id")
            sub_ids = [sub_id] if sub_id else []
            bundled_jobs = []

            if assign_id:
                # Bundle any other queued/retrying UPLOAD_ANNOTATION jobs for the same assignment to minimize NAS I/O
                other_jobs = (
                    db.query(SyncJob)
                    .filter(
                        SyncJob.job_type == "UPLOAD_ANNOTATION",
                        SyncJob.status.in_(["QUEUED", "RETRYING"]),
                        SyncJob.id != job.id,
                    )
                    .all()
                )
                for oj in other_jobs:
                    if isinstance(oj.payload, dict) and oj.payload.get("assignment_id") == assign_id:
                        s_id = oj.payload.get("submission_id")
                        if s_id:
                            sub_ids.append(s_id)
                            oj.status = "CLAIMED"
                            bundled_jobs.append(oj)
                if bundled_jobs:
                    db.commit()

            if len(sub_ids) > 1:
                logger.info(f"Bundled {len(sub_ids)} pending upload jobs for assignment {assign_id} into 1 single batch transfer.")
                upload_and_verify_annotations_batch(db, sub_ids, mock_base_dir=p)
                for oj in bundled_jobs:
                    oj.status = "COMPLETED"
                    oj.error = None
            else:
                upload_and_verify_annotation(db, job.payload["submission_id"], mock_base_dir=p)
        elif job.job_type == "CLEANUP_EXPIRED":
            logger.info("Executed cleanup task.")
        else:
            raise ValueError(f"Unknown job type: {job.job_type}")

        job.status = "COMPLETED"
        job.error = None
        db.commit()
        logger.info(f"Job {job.id} completed successfully.")

    except Exception as exc:
        db.rollback()
        job.attempts += 1
        job.error = str(exc)
        logger.error(f"Job {job.id} failed: {exc}", exc_info=True)

        if job.attempts >= job.max_retries:
            job.status = "FAILED"
            logger.error(f"Job {job.id} reached maximum retries ({job.max_retries}); marked FAILED.")
            if job.job_type == "PREPARE_ASSIGNMENT":
                assign_id = job.payload.get("assignment_id")
                if assign_id:
                    assign = db.query(Assignment).filter(Assignment.id == assign_id).first()
                    if assign and assign.status == "PREPARING":
                        assign.status = "FAILED"
            elif job.job_type == "CATALOG_SCAN":
                dataset_id = job.payload.get("dataset_id")
                if dataset_id:
                    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
                    if ds and ds.catalog_state == "SCANNING":
                        ds.catalog_state = "FAILED"
        else:
            job.status = "RETRYING"
            # Exponential backoff: 10s, 20s, 40s, 80s... max 900s
            backoff_sec = min(900, 10 * (2 ** min(job.attempts, 6)))
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_sec)
            logger.info(f"Job {job.id} scheduled for retry in {backoff_sec}s.")

        db.commit()


def run_worker_once(mock_base_dir: Optional[str] = None) -> bool:
    """Run one single job pass. Useful for tests or cron invocations."""
    worker_name = get_worker_name()
    db = SessionLocal()
    try:
        job = claim_next_job(db, worker_name)
        if not job:
            return False
        execute_job(db, job, mock_base_dir=mock_base_dir)
        return True
    finally:
        db.close()


def run_worker_loop():
    logger.info("Starting Sync Worker daemon loop...")
    try:
        from controller.app.nas_service import sync_rclone_config_from_env
        sync_rclone_config_from_env()
    except Exception as exc:
        logger.warning(f"Could not synchronize Rclone config from .env on worker start: {exc}")

    worker_name = get_worker_name()
    while True:
        db = SessionLocal()
        try:
            job = claim_next_job(db, worker_name)
            if job:
                execute_job(db, job)
            else:
                time.sleep(2)
        except Exception as exc:
            logger.error(f"Worker loop exception: {exc}")
            time.sleep(5)
        finally:
            db.close()


if __name__ == "__main__":
    run_worker_loop()

