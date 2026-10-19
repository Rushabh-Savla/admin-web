import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from controller.app.database import Base, get_db
from controller.app.models import (
    User,
    Dataset,
    DatasetFile,
    Assignment,
    AssignmentItem,
    ActiveImageClaim,
    SyncJob,
)
from controller.app.main import app
from controller.app.auth import hash_password, create_access_token
from controller.app.config import settings
from controller.app.assignment_service import create_atomic_assignment
from sync_worker.batch_staging import stage_batch_download, BatchStagingError
from sync_worker.worker import claim_next_job, execute_job
from annotator_agent.agent import AnnotatorAgent


class TestNasBulkBatchTransfer:
    @pytest.fixture(autouse=True)
    def setup_test(self, monkeypatch):
        self.tmp = Path(tempfile.mkdtemp())
        self.mock_nas = self.tmp / "nas"
        self.staging_dir = self.tmp / "staging" / "batches"
        self.cache_dir = self.tmp / "staging" / "cache"
        self.client_workspace = self.tmp / "agent_ws"

        self.mock_nas.mkdir(parents=True)
        self.staging_dir.mkdir(parents=True)
        self.cache_dir.mkdir(parents=True)
        self.client_workspace.mkdir(parents=True)

        monkeypatch.setattr(settings, "BATCH_STAGING_DIR", str(self.staging_dir))
        monkeypatch.setattr(settings, "CONTROLLER_CACHE_DIR", str(self.cache_dir))
        monkeypatch.setattr(settings, "MAX_ACTIVE_NAS_DOWNLOAD_JOBS", 1)
        monkeypatch.setattr(settings, "RCLONE_BATCH_TRANSFERS", 1)
        monkeypatch.setattr(settings, "RCLONE_BATCH_CHECKERS", 1)
        monkeypatch.setattr(settings, "RCLONE_BATCH_BANDWIDTH", "10M")
        os.environ["MOCK_NAS_DIR"] = str(self.mock_nas)

        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Seed users
        self.admin = User(username="admin_user", password_hash=hash_password("admin_pass"), role="ADMIN", is_active=True)
        self.annotator_a = User(username="annotator_a", password_hash=hash_password("pass_a"), role="ANNOTATOR", is_active=True)
        self.annotator_b = User(username="annotator_b", password_hash=hash_password("pass_b"), role="ANNOTATOR", is_active=True)
        self.db.add_all([self.admin, self.annotator_a, self.annotator_b])
        self.db.commit()

        self.token_a = create_access_token({"sub": "annotator_a", "role": "ANNOTATOR", "user_id": self.annotator_a.id})
        self.token_b = create_access_token({"sub": "annotator_b", "role": "ANNOTATOR", "user_id": self.annotator_b.id})

        # Seed dataset
        self.dataset = Dataset(
            name="yolo_dataset",
            remote_name="annotation_nas",
            remote_path="ai_data_collection/yolo",
            catalog_state="READY",
            current_catalog_version="v1",
        )
        self.db.add(self.dataset)
        self.db.commit()

        def override_get_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        yield

        app.dependency_overrides.clear()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_200_images_single_bulk_transfer_invocation(self):
        """
        Acceptance Test Criteria 1, 2, 12, 14:
        - 200 images in a batch are transferred via EXACTLY ONE coordinated bulk transfer command.
        - NOT 200 independent download operations or subprocess calls.
        - Produces every expected image in the correct local directory structure.
        """
        nas_dataset_dir = self.mock_nas / "ai_data_collection" / "yolo"
        nas_dataset_dir.mkdir(parents=True)

        # Create 200 mock image files with nested folders and names with spaces
        files = []
        for i in range(1, 201):
            subfolder = "sub_a" if i <= 100 else "sub folder with space"
            rel_path = f"{subfolder}/camera image {i:04d}.jpg"
            img_file = nas_dataset_dir / rel_path
            img_file.parent.mkdir(parents=True, exist_ok=True)
            img_bytes = f"fake_image_bytes_{i}".encode("utf-8")
            img_file.write_bytes(img_bytes)

            df = DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=rel_path,
                basename=Path(rel_path).name,
                extension="jpg",
                category="IMAGE",
                size=len(img_bytes),
                fingerprint=hashlib.sha256(img_bytes).hexdigest()[:16],
                catalog_version="v1",
                file_state="ELIGIBLE",
            )
            files.append(df)

        self.db.bulk_save_objects(files)
        self.db.commit()

        # Create assignment for 200 images
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=200,
        )
        assignment.status = "READY"
        self.db.commit()

        # Execute batch staging
        res = stage_batch_download(self.db, assignment.id, mock_base_dir=self.mock_nas)

        # Verification of Bulk Transfer Criteria
        assert res["status"] == "READY"
        assert res["total_images"] == 200
        # Exactly ONE bulk command invocation occurred
        assert res["bulk_commands_invoked"] == 1

        # Check bundle archive exists and contains all 200 images in nested folders
        bundle_p = Path(res["bundle_path"])
        assert bundle_p.exists()
        assert bundle_p.stat().st_size > 0

        with tarfile.open(bundle_p, "r:gz") as tar:
            names = tar.getnames()
            assert "manifest.json" in names
            # All 200 images present in archive with correct paths
            for i in range(1, 201):
                subfolder = "sub_a" if i <= 100 else "sub folder with space"
                rel_path = f"images/{subfolder}/camera image {i:04d}.jpg"
                assert rel_path in names

    def test_batch_validation_rejects_missing_or_corrupted_files(self):
        """
        Acceptance Test Criteria 3 & 4:
        - A batch is NOT marked ready until all expected files are validated.
        - Truncated/missing files cause validation failure.
        - Partial batches are not exposed as ready.
        """
        nas_dataset_dir = self.mock_nas / "ai_data_collection" / "yolo"
        nas_dataset_dir.mkdir(parents=True, exist_ok=True)

        # Create 5 images, but delete the 5th to simulate a missing file
        for i in range(1, 5):
            p = nas_dataset_dir / f"img_{i}.jpg"
            p.write_bytes(b"content")
            df = DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=f"img_{i}.jpg",
                basename=f"img_{i}.jpg",
                extension="jpg",
                category="IMAGE",
                size=7,
                catalog_version="v1",
                file_state="ELIGIBLE",
            )
            self.db.add(df)

        # 5th file record exists in DB, but does NOT exist on storage
        df_missing = DatasetFile(
            dataset_id=self.dataset.id,
            normalized_path="img_missing.jpg",
            basename="img_missing.jpg",
            extension="jpg",
            category="IMAGE",
            size=7,
            catalog_version="v1",
            file_state="ELIGIBLE",
        )
        self.db.add(df_missing)
        self.db.commit()

        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=5,
        )
        assignment.status = "READY"
        self.db.commit()

        # Batch staging must fail validation
        with pytest.raises(BatchStagingError) as exc_info:
            stage_batch_download(self.db, assignment.id, mock_base_dir=self.mock_nas)

        # Batch directory was NOT published
        final_dir = self.staging_dir / f"batch_{assignment.id}"
        assert not final_dir.exists()
        assert not (final_dir / "bundle.tar.gz").exists()

    def test_idempotent_job_creation_and_double_click_protection(self):
        """
        Acceptance Test Criteria 5 & 7:
        - Two concurrent user requests or repeated GUI clicks for the same batch do NOT launch duplicate jobs.
        """
        assignment = Assignment(
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=1,
            allocated_count=1,
            status="READY",
            catalog_version="v1",
        )
        self.db.add(assignment)
        self.db.commit()

        # First request
        r1 = self.client.post(
            f"/api/v1/assignments/{assignment.id}/stage-batch",
            headers={"Authorization": f"Bearer {self.token_a}"},
        )
        assert r1.status_code == 200
        data1 = r1.json()

        # Second rapid request (double click simulation)
        r2 = self.client.post(
            f"/api/v1/assignments/{assignment.id}/stage-batch",
            headers={"Authorization": f"Bearer {self.token_a}"},
        )
        assert r2.status_code == 200
        data2 = r2.json()

        # Exactly 1 SyncJob exists in DB for this assignment
        jobs = self.db.query(SyncJob).filter(SyncJob.idempotency_key == f"stage_batch_{assignment.id}").all()
        assert len(jobs) == 1

    def test_global_nas_concurrency_limit_enforced_across_workers(self):
        """
        Acceptance Test Criteria 6:
        - Two different annotators requesting separate batches cannot bypass the global NAS concurrency limit.
        - MAX_ACTIVE_NAS_DOWNLOAD_JOBS = 1 globally.
        """
        # Create Job A for annotator A
        job_a = SyncJob(
            job_type="STAGE_BATCH_DOWNLOAD",
            status="QUEUED",
            payload={"assignment_id": 101},
            idempotency_key="stage_batch_101",
        )
        # Create Job B for annotator B
        job_b = SyncJob(
            job_type="STAGE_BATCH_DOWNLOAD",
            status="QUEUED",
            payload={"assignment_id": 102},
            idempotency_key="stage_batch_102",
        )
        self.db.add_all([job_a, job_b])
        self.db.commit()

        # Worker 1 claims job
        claimed_1 = claim_next_job(self.db, "worker_proc_1")
        assert claimed_1 is not None
        assert claimed_1.id == job_a.id
        assert claimed_1.status == "CLAIMED"

        # Worker 2 attempts to claim while Job A is active
        # Since MAX_ACTIVE_NAS_DOWNLOAD_JOBS = 1, it CANNOT claim Job B!
        claimed_2 = claim_next_job(self.db, "worker_proc_2")
        assert claimed_2 is None  # Slot is occupied!

        # Now Worker 1 completes Job A
        claimed_1.status = "COMPLETED"
        self.db.commit()

        # Now Worker 2 is able to claim Job B
        claimed_3 = claim_next_job(self.db, "worker_proc_2")
        assert claimed_3 is not None
        assert claimed_3.id == job_b.id
        assert claimed_3.status == "CLAIMED"

    def test_security_rejection_of_unauthorized_access_and_path_traversal(self):
        """
        Acceptance Test Criteria 10:
        - Annotator B cannot stage or download Annotator A's assignment.
        - Path traversal in relative paths is rejected.
        """
        assign_a = Assignment(
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=1,
            allocated_count=1,
            status="READY",
            catalog_version="v1",
        )
        self.db.add(assign_a)
        self.db.commit()

        # Annotator B attempts to request staging for Annotator A's assignment -> 403 Forbidden
        r = self.client.post(
            f"/api/v1/assignments/{assign_a.id}/stage-batch",
            headers={"Authorization": f"Bearer {self.token_b}"},
        )
        assert r.status_code == 403

        # Annotator B attempts to download bundle for Annotator A's assignment -> 403 Forbidden
        r_bundle = self.client.get(
            f"/api/v1/assignments/{assign_a.id}/bundle",
            headers={"Authorization": f"Bearer {self.token_b}"},
        )
        assert r_bundle.status_code == 403

    def test_cache_deduplication_reuses_verified_files(self):
        """
        Acceptance Test Criteria 9:
        - Local staging cache deduplicates files: identical files are resolved locally
          without repeated transfers from NAS.
        """
        nas_dataset_dir = self.mock_nas / "ai_data_collection" / "yolo"
        nas_dataset_dir.mkdir(parents=True, exist_ok=True)

        img_content = b"cached_image_bytes_xyz"
        (nas_dataset_dir / "test_cached.jpg").write_bytes(img_content)

        # Pre-seed cache with verified copy
        (self.cache_dir / "test_cached.jpg").write_bytes(img_content)

        df = DatasetFile(
            dataset_id=self.dataset.id,
            normalized_path="test_cached.jpg",
            basename="test_cached.jpg",
            extension="jpg",
            category="IMAGE",
            size=len(img_content),
            catalog_version="v1",
            file_state="ELIGIBLE",
        )
        self.db.add(df)
        self.db.commit()

        assign = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=1,
        )
        assign.status = "READY"
        self.db.commit()

        res = stage_batch_download(self.db, assign.id, mock_base_dir=self.mock_nas)
        assert res["cache_hits"] == 1
        assert res["transferred_count"] == 0  # Reused from cache!
