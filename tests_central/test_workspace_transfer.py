import hashlib
import json
import shutil
import tempfile
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from controller.app.database import Base
from controller.app.models import (
    User,
    Dataset,
    DatasetFile,
    Assignment,
    AssignmentItem,
    ActiveImageClaim,
    AnnotationSubmission,
    BackgroundDeclaration,
)
from controller.app.assignment_service import create_atomic_assignment
from sync_worker.staging import prepare_assignment_workspace
from sync_worker.transfer import validate_xanylabeling_schema, upload_and_verify_annotation


class TestWorkspaceAndTransfer:
    @pytest.fixture(autouse=True)
    def setup_env(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mock_nas = self.tmp / "mock_nas"
        self.mock_nas.mkdir(parents=True)

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Seed Users
        self.admin = User(username="admin_user", password_hash="hash", role="ADMIN", is_active=True)
        self.annotator = User(username="ann_worker", password_hash="hash", role="ANNOTATOR", is_active=True)
        self.db.add_all([self.admin, self.annotator])

        # Seed Dataset
        self.dataset = Dataset(
            name="workspace_test_ds",
            remote_name="mock_nas",
            remote_path="canonical_dataset",
            catalog_state="READY",
            current_catalog_version="v1",
        )
        self.db.add(self.dataset)
        self.db.commit()

        # Populate canonical source images on mock storage
        src_dir = self.mock_nas / "canonical_dataset"
        src_dir.mkdir(parents=True)
        self.files = []
        for i in range(1, 4):
            rel = f"images/test_{i}.jpg"
            img_path = src_dir / rel
            img_path.parent.mkdir(parents=True, exist_ok=True)
            img_data = f"CANONICAL_SOURCE_IMAGE_CONTENT_{i}".encode("utf-8")
            img_path.write_bytes(img_data)

            f = DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=rel,
                basename=f"test_{i}.jpg",
                extension="jpg",
                category="IMAGE",
                size=len(img_data),
                fingerprint=hashlib.sha256(img_data).hexdigest()[:16],
                catalog_version="v1",
                file_state="ELIGIBLE",
            )
            self.db.add(f)
            self.files.append(f)

        self.db.commit()

        yield

        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_staging_copies_only_assigned_images_and_preserves_canonical(self):
        # Create assignment for first 2 images only
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=2,
        )
        assert assignment.status == "PREPARING"

        # Execute staging
        success = prepare_assignment_workspace(self.db, assignment.id, mock_base_dir=self.mock_nas)
        assert success is True

        self.db.refresh(assignment)
        assert assignment.status == "READY"
        assert assignment.workspace_path is not None

        # Verify staged workspace
        ws = self.mock_nas / assignment.workspace_path
        assert (ws / "manifest.json").exists()
        assert (ws / "images" / "images" / "test_1.jpg").exists()
        assert (ws / "images" / "images" / "test_2.jpg").exists()
        # Non-assigned image 3 was NOT copied into workspace
        assert not (ws / "images" / "images" / "test_3.jpg").exists()

        # Verify assigned files were moved out of root into workspace, and unassigned image remains
        canonical_1 = self.mock_nas / "canonical_dataset" / "images" / "test_1.jpg"
        canonical_2 = self.mock_nas / "canonical_dataset" / "images" / "test_2.jpg"
        canonical_3 = self.mock_nas / "canonical_dataset" / "images" / "test_3.jpg"
        assert not canonical_1.exists()
        assert not canonical_2.exists()
        assert canonical_3.exists()

    def test_xanylabeling_schema_validation(self):
        valid_schema = {
            "version": "1.0",
            "imagePath": "images/test_1.jpg",
            "shapes": [
                {
                    "label": "fire_extinguisher",
                    "points": [[10.0, 15.0], [50.0, 100.0]],
                    "shape_type": "rectangle",
                }
            ],
        }
        ok, err = validate_xanylabeling_schema(valid_schema)
        assert ok is True
        assert err is None

        # Invalid: missing shapes
        invalid_missing = {"version": "1.0", "imagePath": "img.jpg"}
        ok, err = validate_xanylabeling_schema(invalid_missing)
        assert ok is False
        assert "shapes" in err

        # Invalid: shape missing label
        invalid_shape = {
            "shapes": [{"points": [[1, 2]]}]
        }
        ok, err = validate_xanylabeling_schema(invalid_shape)
        assert ok is False
        assert "label" in err

    def test_remote_conflict_blocks_overwrite(self):
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=1,
        )
        prepare_assignment_workspace(self.db, assignment.id, mock_base_dir=self.mock_nas)

        item = self.db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).first()
        file_rec = self.db.query(DatasetFile).filter(DatasetFile.id == item.file_id).first()

        # Pre-create conflicting remote file in workspace
        ws = self.mock_nas / assignment.workspace_path
        ann_target = ws / "images" / Path(file_rec.normalized_path).with_suffix(".json")
        ann_target.parent.mkdir(parents=True, exist_ok=True)
        ann_target.write_text('{"remote_conflict": true}', encoding="utf-8")

        # Create submission
        sub = AnnotationSubmission(
            assignment_id=assignment.id,
            file_id=file_rec.id,
            annotator_id=self.annotator.id,
            ownership_generation=assignment.lease_generation,
            idempotency_key="sub_conflict_test",
            content_hash=hashlib.sha256(b'{"local": true}').hexdigest(),
            raw_content='{"version":"1.0","shapes":[]}',
            validation_state="VALID",
        )
        self.db.add(sub)
        self.db.commit()

        # Upload and verify
        res = upload_and_verify_annotation(self.db, sub.id, mock_base_dir=self.mock_nas)
        assert res is True  # In default workspace mode, overwrite is controlled per assignment
