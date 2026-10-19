import hashlib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from controller.app.models import (
    Assignment,
    AssignmentItem,
    DatasetFile,
    AnnotationSubmission,
    ActiveImageClaim,
    Dataset,
)
from controller.app.config import settings

logger = logging.getLogger(__name__)


def validate_xanylabeling_schema(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Validate that dictionary adheres to XAnyLabeling annotation JSON schema.
    Must contain 'shapes' list, imagePath or valid keys, and preserved fields.
    """
    if not isinstance(data, dict):
        return False, "Root annotation payload must be a JSON object"

    if "shapes" not in data or not isinstance(data["shapes"], list):
        return False, "Missing or invalid 'shapes' list in annotation JSON"

    for idx, shape in enumerate(data["shapes"]):
        if not isinstance(shape, dict):
            return False, f"Shape at index {idx} is not an object"
        if "label" not in shape or not isinstance(shape["label"], str):
            return False, f"Shape at index {idx} is missing valid string 'label'"
        if "points" not in shape or not isinstance(shape["points"], list):
            return False, f"Shape at index {idx} is missing 'points' coordinate list"

    return True, None


def upload_and_verify_annotation(
    db: Session,
    submission_id: int,
    mock_base_dir: Optional[Path] = None,
) -> bool:
    """
    Process an annotation submission:
    1. Verify ownership generation and lease.
    2. Check for remote conflicts.
    3. Write and verify upload on storage target.
    4. Update item status to VERIFIED_COMPLETE.
    5. Check if entire assignment is finished and mark COMPLETED.
    """
    sub = db.query(AnnotationSubmission).filter(AnnotationSubmission.id == submission_id).first()
    if not sub:
        raise ValueError(f"Submission {submission_id} not found")

    assignment = db.query(Assignment).filter(Assignment.id == sub.assignment_id).first()
    if not assignment:
        raise ValueError("Assignment missing for submission")

    if assignment.lease_generation != sub.ownership_generation:
        sub.validation_state = "STALE_GENERATION"
        sub.error = f"Submission ownership generation {sub.ownership_generation} does not match active lease {assignment.lease_generation}"
        db.commit()
        raise ValueError(sub.error)

    item = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.file_id == sub.file_id,
        )
        .first()
    )
    if not item:
        raise ValueError("Assignment item missing")

    file_record = db.query(DatasetFile).filter(DatasetFile.id == sub.file_id).first()
    dataset = db.query(Dataset).filter(Dataset.id == assignment.dataset_id).first()
    if not file_record or not dataset:
        raise ValueError("File or Dataset record missing")

    # Target path calculation
    # e.g., image: folder/sub/img01.jpg -> json: folder/sub/img01.json
    image_p = Path(file_record.normalized_path)
    json_rel_path = image_p.with_suffix(".json").as_posix()

    rclone_cfg = Path(settings.RCLONE_CONFIG_PATH)
    is_mock = mock_base_dir is not None or not rclone_cfg.exists() or rclone_cfg.stat().st_size == 0

    if settings.ANNOTATION_OUTPUT_POLICY == "SIDECAR_PUBLISH":
        target_rel_dir = dataset.remote_path.strip("/")
    else:
        # Save JSON directly in images directory alongside the image files
        target_rel_dir = f"{assignment.workspace_path}/images"

    target_json_path = f"{target_rel_dir}/{json_rel_path}"

    payload_bytes = sub.raw_content.encode("utf-8") if sub.raw_content else b"{}"
    calc_hash = hashlib.sha256(payload_bytes).hexdigest()

    if is_mock:
        base_path = mock_base_dir if mock_base_dir else Path("/tmp/nas_annotation_mock")
        final_file = (base_path / target_json_path).resolve()
        final_file.parent.mkdir(parents=True, exist_ok=True)

        # Conflict check if publishing to dataset root
        if settings.ANNOTATION_OUTPUT_POLICY == "SIDECAR_PUBLISH" and final_file.exists():
            existing_hash = hashlib.sha256(final_file.read_bytes()).hexdigest()
            if existing_hash != calc_hash:
                item.item_state = "CONFLICT"
                item.error = "Remote sidecar annotation exists and has differing content"
                sub.validation_state = "CONFLICT"
                sub.error = item.error
                db.commit()
                return False

        final_file.write_bytes(payload_bytes)

        # Verify
        if not final_file.exists() or final_file.stat().st_size != len(payload_bytes):
            raise RuntimeError("Verification failed: written file size mismatch")

    else:
        # Real Rclone upload
        import tempfile
        with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
            tf.write(payload_bytes)
            tmp_local = tf.name

        try:
            remote_target = f"{dataset.remote_name}:{target_json_path}"
            copy_cmd = [
                settings.RCLONE_BIN,
                "copyto",
                tmp_local,
                remote_target,
            ]
            if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                copy_cmd += ["--config", settings.RCLONE_CONFIG_PATH]

            res = subprocess.run(copy_cmd, capture_output=True, text=True, timeout=120)
            if res.returncode != 0:
                raise RuntimeError(f"Rclone annotation upload failed: {res.stderr[:500]}")

            # Verify existence and size via lsjson
            v_cmd = [
                settings.RCLONE_BIN,
                "lsjson",
                remote_target,
            ]
            if settings.RCLONE_CONFIG_PATH and Path(settings.RCLONE_CONFIG_PATH).exists():
                v_cmd += ["--config", settings.RCLONE_CONFIG_PATH]

            vres = subprocess.run(v_cmd, capture_output=True, text=True, timeout=60)
            if vres.returncode != 0:
                raise RuntimeError(f"Rclone verification failed: {vres.stderr[:500]}")

            vdata = json.loads(vres.stdout or "[]")
            if not vdata or int(vdata[0].get("Size", -1)) != len(payload_bytes):
                raise RuntimeError("Uploaded annotation size mismatch on remote target")

        finally:
            Path(tmp_local).unlink(missing_ok=True)

    # Mark item completed
    item.item_state = "VERIFIED_COMPLETE"
    item.error = None
    sub.validation_state = "VALID"
    sub.error = None
    db.flush()

    # Check if all items in assignment are completed
    uncompleted_count = (
        db.query(AssignmentItem)
        .filter(
            AssignmentItem.assignment_id == assignment.id,
            AssignmentItem.item_state.in_(["PENDING", "DOWNLOADING", "ANNOTATING"]),
        )
        .count()
    )

    if uncompleted_count == 0 and assignment.status in ["IN_PROGRESS", "READY", "PREPARING"]:
        assignment.status = "SUBMITTED_FOR_REVIEW"
        logger.info(f"Assignment {assignment.id} fully uploaded & verified. Marked SUBMITTED_FOR_REVIEW awaiting master approval.")

    db.commit()
    return True
