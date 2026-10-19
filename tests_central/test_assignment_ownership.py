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
)
from controller.app.assignment_service import (
    create_atomic_assignment,
    pause_assignment,
    resume_assignment,
    cancel_assignment,
)


class TestAssignmentOwnership:
    @pytest.fixture(autouse=True)
    def setup_db(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Seed Users
        self.admin = User(username="admin_user", password_hash="hash", role="ADMIN", is_active=True)
        self.annotator_a = User(username="annotator_a", password_hash="hash", role="ANNOTATOR", is_active=True)
        self.annotator_b = User(username="annotator_b", password_hash="hash", role="ANNOTATOR", is_active=True)
        self.disabled_user = User(username="disabled_user", password_hash="hash", role="ANNOTATOR", is_active=False)
        self.db.add_all([self.admin, self.annotator_a, self.annotator_b, self.disabled_user])

        # Seed Dataset
        self.dataset = Dataset(
            name="test_dataset_7k",
            remote_name="mock_nas",
            remote_path="data/7k",
            catalog_state="READY",
            current_catalog_version="cat_v1_hash",
        )
        self.db.add(self.dataset)
        self.db.commit()

        yield

        self.db.close()

    def test_7000_images_allocation_sequence(self):
        """
        Acceptance test:
        - 7,000 mock image records;
        - Assign 4,000 to A -> exactly 4,000 claims and 3,000 available;
        - Request 4,000 for B -> entire request rejected, zero partial records, 3,000 available;
        - Assign 2,000 to B -> 2,000 claims and 1,000 available;
        - No active image appears in two owners.
        """
        # Populate 7,000 mock image records
        files = [
            DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=f"batch_folder/img_{i:05d}.jpg",
                basename=f"img_{i:05d}.jpg",
                extension="jpg",
                category="IMAGE",
                size=1024,
                catalog_version="cat_v1_hash",
                file_state="ELIGIBLE",
            )
            for i in range(1, 7001)
        ]
        self.db.bulk_save_objects(files)
        self.db.commit()

        # Verify initial available count
        total = self.db.query(DatasetFile).filter(DatasetFile.dataset_id == self.dataset.id).count()
        assert total == 7000

        # Step 1: Assign 4,000 to Annotator A
        assign_a = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch_folder",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=4000,
        )
        assert assign_a.allocated_count == 4000
        assert assign_a.status == "PREPARING"

        # Verify A owns 4,000, exactly 3,000 available
        claims_a = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.annotator_id == self.annotator_a.id).count()
        assert claims_a == 4000

        unclaimed_count = (
            self.db.query(DatasetFile)
            .filter(
                DatasetFile.dataset_id == self.dataset.id,
                ~DatasetFile.id.in_(self.db.query(ActiveImageClaim.file_id)),
            )
            .count()
        )
        assert unclaimed_count == 3000

        # Step 2: Attempt 4,000 for Annotator B -> MUST fail and leave 3,000 available
        with pytest.raises(ValueError) as exc_info:
            create_atomic_assignment(
                db=self.db,
                dataset_id=self.dataset.id,
                directory_scope="batch_folder",
                scope_type="RECURSIVE",
                annotator_id=self.annotator_b.id,
                creator_id=self.admin.id,
                requested_count=4000,
            )
        assert "requested 4000 images, but only 3000" in str(exc_info.value)

        # Confirm zero partial rows created for B
        claims_b_zero = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.annotator_id == self.annotator_b.id).count()
        assert claims_b_zero == 0

        unclaimed_after_fail = (
            self.db.query(DatasetFile)
            .filter(
                DatasetFile.dataset_id == self.dataset.id,
                ~DatasetFile.id.in_(self.db.query(ActiveImageClaim.file_id)),
            )
            .count()
        )
        assert unclaimed_after_fail == 3000

        # Step 3: Assign 2,000 to Annotator B -> B owns 2,000, 1,000 remain available
        assign_b = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch_folder",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_b.id,
            creator_id=self.admin.id,
            requested_count=2000,
        )
        assert assign_b.allocated_count == 2000

        claims_b = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.annotator_id == self.annotator_b.id).count()
        assert claims_b == 2000

        unclaimed_final = (
            self.db.query(DatasetFile)
            .filter(
                DatasetFile.dataset_id == self.dataset.id,
                ~DatasetFile.id.in_(self.db.query(ActiveImageClaim.file_id)),
            )
            .count()
        )
        assert unclaimed_final == 1000

        # Step 4: Strict uniqueness check - no active image appears in two owners
        all_claimed_file_ids = [r[0] for r in self.db.query(ActiveImageClaim.file_id).all()]
        assert len(all_claimed_file_ids) == 6000
        assert len(set(all_claimed_file_ids)) == 6000

    def test_idempotency_key_prevents_duplicate_assignment(self):
        files = [
            DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=f"img_{i}.jpg",
                basename=f"img_{i}.jpg",
                extension="jpg",
                category="IMAGE",
                size=100,
                catalog_version="cat_v1_hash",
                file_state="ELIGIBLE",
            )
            for i in range(1, 11)
        ]
        self.db.bulk_save_objects(files)
        self.db.commit()

        key = "idemp_test_key_123"
        assign1 = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=5,
            idempotency_key=key,
        )

        # Retry with same idempotency key
        assign2 = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=5,
            idempotency_key=key,
        )

        assert assign1.id == assign2.id
        claims = self.db.query(ActiveImageClaim).count()
        assert claims == 5

    def test_disabled_user_rejected(self):
        files = [
            DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path="test.jpg",
                basename="test.jpg",
                extension="jpg",
                category="IMAGE",
                size=100,
                catalog_version="cat_v1_hash",
                file_state="ELIGIBLE",
            )
        ]
        self.db.add_all(files)
        self.db.commit()

        with pytest.raises(ValueError) as exc_info:
            create_atomic_assignment(
                db=self.db,
                dataset_id=self.dataset.id,
                directory_scope="",
                scope_type="RECURSIVE",
                annotator_id=self.disabled_user.id,
                creator_id=self.admin.id,
                requested_count=1,
            )
        assert "disabled" in str(exc_info.value).lower()

    def test_lifecycle_pause_resume_cancel(self):
        files = [
            DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=f"test_{i}.jpg",
                basename=f"test_{i}.jpg",
                extension="jpg",
                category="IMAGE",
                size=100,
                catalog_version="cat_v1_hash",
                file_state="ELIGIBLE",
            )
            for i in range(1, 4)
        ]
        self.db.add_all(files)
        self.db.commit()

        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="",
            scope_type="RECURSIVE",
            annotator_id=self.annotator_a.id,
            creator_id=self.admin.id,
            requested_count=3,
        )

        # Pause
        paused = pause_assignment(self.db, assignment.id, self.admin.id)
        assert paused.status == "PAUSED"

        # Resume
        resumed = resume_assignment(self.db, assignment.id, self.admin.id)
        assert resumed.status == "READY"

        # Cancel - claims released, assignment items preserved
        cancelled = cancel_assignment(self.db, assignment.id, self.admin.id)
        assert cancelled.status == "CANCELLED"

        claims = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims == 0

        # Items still exist historically!
        items = self.db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).count()
        assert items == 3
