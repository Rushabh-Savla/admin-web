import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from controller.app.models import Assignment, AssignmentItem, DatasetFile, User, Dataset, SyncJob
from controller.app.config import settings

logger = logging.getLogger(__name__)


def prepare_assignment_workspace(
    db: Session,
    assignment_id: int,
    mock_base_dir: Optional[Path] = None,
) -> bool:
    """
    Prepare the assignment workspace on the storage target (real NAS via Rclone or mock local directory).
    Stages copies of assigned images into:
      <WORKSPACE_ROOT>/<user_id_slug>/assignment_<assignment_id>/
        manifest.json
        images/
        annotations/
        metadata/
    Verifies that all files exist and match sizes before marking assignment READY.
    """
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise ValueError(f"Assignment {assignment_id} not found")

    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
    annotator = db.query(User).filter(User.id == assignment.annotator_id).first()
    if not dataset or not annotator:
        raise ValueError("Dataset or Annotator missing for assignment")

    # Fetch items with files
    items = (
        db.query(AssignmentItem, DatasetFile)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(AssignmentItem.assignment_id == assignment_id)
        .order_by(AssignmentItem.ordinal.asc())
        .all()
    )

    if not items:
        raise ValueError(f"No items found for assignment {assignment_id}")

    if assignment.workspace_path:
        rel_workspace_dir = assignment.workspace_path
    else:
        nas_main_dir = dataset.remote_path.strip("/") if dataset and dataset.remote_path else settings.ANNOTATION_WORKSPACE_ROOT
        rel_workspace_dir = f"{nas_main_dir}/{annotator.username}/assignment_{assignment.id}"

    # Determine execution mode: Mock directory or real Rclone remote
    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    is_mock = mock_base_dir is not None or not rclone_cfg.exists() or rclone_cfg.stat().st_size == 0

    if is_mock:
        base_path = mock_base_dir if mock_base_dir else Path("/tmp/nas_annotation_mock")
        workspace_full = (base_path / rel_workspace_dir).resolve()
        images_dir = workspace_full / "images"
        metadata_dir = workspace_full / "metadata"

        images_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)

        dataset_source_root = (base_path / dataset.remote_path.lstrip("/")).resolve()

        # Copy assigned images
        staged_manifest_items = []
        for item, file_record in items:
            src_file = dataset_source_root / file_record.normalized_path
            dst_file = images_dir / file_record.normalized_path
            dst_file.parent.mkdir(parents=True, exist_ok=True)

            if src_file.exists():
                shutil.move(str(src_file), str(dst_file))
                actual_size = dst_file.stat().st_size
            elif dst_file.exists():
                actual_size = dst_file.stat().st_size
            else:
                raise FileNotFoundError(f"Source file does not exist on storage: {src_file}")

            staged_manifest_items.append({
                "item_id": item.id,
                "file_id": file_record.id,
                "ordinal": item.ordinal,
                "relative_path": file_record.normalized_path,
                "size": actual_size,
                "fingerprint": file_record.fingerprint,
            })

        # Verification step
        verified_count = 0
        for m_item in staged_manifest_items:
            check_path = images_dir / m_item["relative_path"]
            if check_path.exists() and check_path.stat().st_size > 0:
                verified_count += 1

        if verified_count != len(items):
            raise RuntimeError(
                f"Workspace verification failed: expected {len(items)} images, but only verified {verified_count}"
            )

        # Write manifest.json
        manifest_data = {
            "assignment_id": assignment.id,
            "catalog_version": assignment.catalog_version,
            "annotator_id": annotator.id,
            "dataset_id": dataset.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "item_count": len(items),
            "items": staged_manifest_items,
        }
        (workspace_full / "manifest.json").write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        # Prefetch the exact assignment file set into the shared server SSD cache.
        cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache")).resolve()
        cache_root = (cache_base / f"assignment_{assignment.id}").resolve()
        cache_images = cache_root / "images"
        cache_images.mkdir(parents=True, exist_ok=True)

        cache_manifest_items = []
        for item, file_record in items:
            source = images_dir / file_record.normalized_path
            destination = cache_images / file_record.normalized_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source), str(destination))
            actual_size = destination.stat().st_size
            if actual_size != file_record.size:
                raise RuntimeError(
                    f"Cache verification size mismatch for {file_record.normalized_path}: "
                    f"expected {file_record.size}, found {actual_size}"
                )
            cache_manifest_items.append({
                "item_id": item.id,
                "file_id": file_record.id,
                "ordinal": item.ordinal,
                "relative_path": file_record.normalized_path,
                "size": actual_size,
                "fingerprint": file_record.fingerprint,
            })

        cache_manifest = {
            "assignment_id": assignment.id,
            "catalog_version": assignment.catalog_version,
            "annotator_id": annotator.id,
            "dataset_id": dataset.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "item_count": len(cache_manifest_items),
            "items": cache_manifest_items,
        }
        _write_assignment_cache_manifest(cache_root, cache_manifest)
        _write_ready_marker(cache_root)

        assignment.workspace_path = rel_workspace_dir
        assignment.status = "READY"
        db.commit()
        logger.info(f"Assignment {assignment_id} successfully staged and marked READY (mock mode).")
        return True

    else:
        # Real Rclone NAS staging
        clean_remote_path = dataset.remote_path.strip("/")
        remote_source = f"{dataset.remote_name}:{clean_remote_path}"
        remote_dest = f"{dataset.remote_name}:{rel_workspace_dir}"

        all_have_images_prefix = all(f.normalized_path.startswith("images/") for _, f in items)
        move_dest = remote_dest if all_have_images_prefix else f"{remote_dest}/images"

        manifest_items = [{
            "item_id": item.id,
            "file_id": file_record.id,
            "ordinal": item.ordinal,
            "relative_path": file_record.normalized_path,
            "size": file_record.size,
            "fingerprint": file_record.fingerprint,
        } for item, file_record in items]

        import tempfile
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            for _, file_record in items:
                tf.write(file_record.normalized_path + "\n")
            list_file_path = tf.name

        cache_base = Path(os.getenv("CONTROLLER_CACHE_DIR", "/tmp/nas_cache")).resolve()
        cache_root = (cache_base / f"assignment_{assignment.id}").resolve()
        cache_images = cache_root / "images"
        cache_images.mkdir(parents=True, exist_ok=True)

        try:
            # Move only the exact assignment file list into the NAS workspace.
            move_cmd = [
                settings.RCLONE_BIN,
                "move",
                remote_source,
                move_dest,
                "--files-from-raw", list_file_path,
                "--transfers", str(settings.RCLONE_TRANSFERS),
                "--checkers", str(settings.RCLONE_CHECKERS),
                "--timeout", settings.RCLONE_TIMEOUT,
                "--contimeout", settings.RCLONE_CONNECT_TIMEOUT,
            ]
            if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                move_cmd += ["--config", settings.RCLONE_CONFIG_PATH]

            move_res = subprocess.run(move_cmd, capture_output=True, text=True, timeout=settings.TRANSFER_TIMEOUT_SECONDS)
            if move_res.returncode != 0:
                raise RuntimeError(f"Rclone move failed (code {move_res.returncode}): {move_res.stderr[:500]}")

            # Batch-copy only the exact assignment file list into the shared SSD cache.
            all_have_images_prefix = all(f.normalized_path.startswith("images/") for _, f in items)
            cache_remote_target = (
                f"{dataset.remote_name}:{rel_workspace_dir}"
                if all_have_images_prefix
                else f"{dataset.remote_name}:{rel_workspace_dir}/images"
            )
            cache_cmd = [
                settings.RCLONE_BIN,
                "copy",
                cache_remote_target,
                str(cache_images),
                "--files-from-raw", list_file_path,
                "--transfers", str(settings.RCLONE_TRANSFERS),
                "--checkers", str(settings.RCLONE_CHECKERS),
                "--timeout", settings.RCLONE_TIMEOUT,
                "--contimeout", settings.RCLONE_CONNECT_TIMEOUT,
            ]
            if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                cache_cmd += ["--config", settings.RCLONE_CONFIG_PATH]

            cache_res = subprocess.run(cache_cmd, capture_output=True, text=True, timeout=settings.TRANSFER_TIMEOUT_SECONDS)
            if cache_res.returncode != 0:
                raise RuntimeError(f"Rclone SSD cache copy failed (code {cache_res.returncode}): {cache_res.stderr[:500]}")

            # Verify only the exact manifest entries; no recursive lsjson listing is needed.
            if len(manifest_items) != len(items):
                raise RuntimeError("Assignment manifest item count mismatch")

            for m_item in manifest_items:
                relative_path = m_item["relative_path"]
                cache_path = (cache_images / relative_path).resolve()
                try:
                    cache_path.relative_to(cache_images.resolve())
                except ValueError:
                    raise RuntimeError(f"Unsafe assignment cache path: {relative_path}")
                if not cache_path.is_file():
                    raise RuntimeError(f"Cached image missing: {relative_path}")
                actual_size = cache_path.stat().st_size
                expected_size = m_item["size"]
                if actual_size != expected_size:
                    raise RuntimeError(
                        f"Cache verification size mismatch for {relative_path}: "
                        f"expected {expected_size}, found {actual_size}"
                    )

            manifest_content = {
                "assignment_id": assignment.id,
                "catalog_version": assignment.catalog_version,
                "annotator_id": annotator.id,
                "dataset_id": dataset.id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "item_count": len(manifest_items),
                "items": manifest_items,
            }

            _write_assignment_cache_manifest(cache_root, manifest_content)

            # Upload the same exact manifest to the NAS workspace.
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as mf:
                mf.write(json.dumps(manifest_content, indent=2))
                mf_path = mf.name

            try:
                m_upload_cmd = [
                    settings.RCLONE_BIN,
                    "copyto",
                    mf_path,
                    f"{remote_dest}/manifest.json",
                    "--timeout", settings.RCLONE_TIMEOUT,
                    "--contimeout", settings.RCLONE_CONNECT_TIMEOUT,
                ]
                if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                    m_upload_cmd += ["--config", settings.RCLONE_CONFIG_PATH]
                manifest_res = subprocess.run(m_upload_cmd, capture_output=True, text=True, timeout=120)
                if manifest_res.returncode != 0:
                    raise RuntimeError(
                        f"Rclone manifest upload failed (code {manifest_res.returncode}): {manifest_res.stderr[:500]}"
                    )
            finally:
                Path(mf_path).unlink(missing_ok=True)

            _write_ready_marker(cache_root)
            assignment.workspace_path = rel_workspace_dir
            assignment.status = "READY"
            db.commit()
            logger.info(f"Assignment {assignment_id} staged, cached, and confirmed READY via Rclone.")
            return True

        finally:
            Path(list_file_path).unlink(missing_ok=True)


def _write_assignment_cache_manifest(cache_root: Path, manifest_data: Dict) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "manifest.json").write_text(
        json.dumps(manifest_data, indent=2),
        encoding="utf-8",
    )


def _write_ready_marker(cache_root: Path) -> None:
    (cache_root / ".ready").write_text("ready\n", encoding="utf-8")

