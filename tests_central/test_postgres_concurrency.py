import os
import concurrent.futures
from typing import List
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
    SyncJob,
    AuditEvent,
)
from controller.app.assignment_service import create_atomic_assignment


# If POSTGRES_TEST_URL is not set, this test will be skipped
TEST_PG_URL = os.getenv("POSTGRES_TEST_URL", "")


@pytest.mark.skipif(not TEST_PG_URL, reason="Requires real PostgreSQL instance configured via POSTGRES_TEST_URL")
class TestPostgresConcurrency:
    @pytest.fixture(autouse=True)
    def setup_pg(self):
        self.engine = create_engine(TEST_PG_URL, pool_size=20, max_overflow=20)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        with self.Session() as db:
            # Wipe any previous test residue
            db.query(ActiveImageClaim).delete()
            db.query(AssignmentItem).delete()
            db.query(Assignment).delete()
            db.query(SyncJob).delete()
            db.query(AuditEvent).delete()
            db.query(DatasetFile).delete()
            db.query(Dataset).delete()
            db.query(User).delete()
            db.commit()

            # Seed admin and 5 annotators
            admin = User(username="admin_concur", password_hash="hash", role="ADMIN", is_active=True)
            db.add(admin)
            annotators = []
            for i in range(1, 6):
                ann = User(username=f"annotator_concur_{i}", password_hash="hash", role="ANNOTATOR", is_active=True)
                db.add(ann)
                annotators.append(ann)

            dataset = Dataset(
                name="pg_concur_ds",
                remote_name="nas",
                remote_path="data/concur",
                catalog_state="READY",
                current_catalog_version="v_concur_1",
            )
            db.add(dataset)
            db.flush()

            self.admin_id = admin.id
            self.dataset_id = dataset.id
            self.annotator_ids = [a.id for a in annotators]

            # Populate 5,000 eligible images
            files = [
                DatasetFile(
                    dataset_id=dataset.id,
                    normalized_path=f"folder/img_{idx:05d}.jpg",
                    basename=f"img_{idx:05d}.jpg",
                    extension="jpg",
                    category="IMAGE",
                    size=1024,
                    catalog_version="v_concur_1",
                    file_state="ELIGIBLE",
                )
                for idx in range(1, 5001)
            ]
            db.bulk_save_objects(files)
            db.commit()

        yield

        # Teardown
        with self.Session() as db:
            db.query(ActiveImageClaim).delete()
            db.query(AssignmentItem).delete()
            db.query(Assignment).delete()
            db.query(SyncJob).delete()
            db.query(AuditEvent).delete()
            db.query(DatasetFile).delete()
            db.query(Dataset).delete()
            db.query(User).delete()
            db.commit()

    def test_concurrent_overlapping_assignments(self):
        """
        Concurrency test against real PostgreSQL:
        5 concurrent worker threads attempt to reserve 1,500 images each from a pool of 5,000.
        Exactly 3 workers must succeed (4,500 images reserved), and 2 workers must fail completely.
        Zero partial assignments, and zero duplicate claims!
        """
        def worker_attempt(annotator_id: int):
            session = self.Session()
            try:
                assignment = create_atomic_assignment(
                    db=session,
                    dataset_id=self.dataset_id,
                    directory_scope="folder",
                    scope_type="RECURSIVE",
                    annotator_id=annotator_id,
                    creator_id=self.admin_id,
                    requested_count=1500,
                )
                return {"success": True, "assignment_id": assignment.id, "error": None}
            except Exception as exc:
                return {"success": False, "assignment_id": None, "error": str(exc)}
            finally:
                session.close()

        # Run 5 threads concurrently
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker_attempt, ann_id) for ann_id in self.annotator_ids]
            for f in concurrent.futures.as_completed(futures):
                results.append(f.result())

        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]

        # 5000 images / 1500 per assignment = exactly 3 can succeed
        assert len(successes) == 3, f"Expected exactly 3 successes, got {len(successes)}: {results}"
        assert len(failures) == 2, f"Expected exactly 2 failures, got {len(failures)}"

        with self.Session() as db:
            # Verify total active claims equals 4,500
            total_claims = db.query(ActiveImageClaim).count()
            assert total_claims == 4500

            # Verify no duplicates
            distinct_claims = db.query(ActiveImageClaim.file_id).distinct().count()
            assert distinct_claims == 4500

            # Verify remaining available count is exactly 500
            claimed_ids = db.query(ActiveImageClaim.file_id)
            unclaimed = db.query(DatasetFile).filter(~DatasetFile.id.in_(claimed_ids)).count()
            assert unclaimed == 500
