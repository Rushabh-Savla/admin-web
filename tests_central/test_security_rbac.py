import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from controller.app.database import Base, get_db
from controller.app.models import User, Dataset, DatasetFile, Assignment, AssignmentItem
from controller.app.main import app
from controller.app.auth import hash_password, create_access_token
from controller.app.config import settings


class TestSecurityAndRBAC:
    @pytest.fixture(autouse=True)
    def setup_app(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()

        # Seed test users
        self.admin = User(username="admin_sec", password_hash=hash_password("admin_pass123"), role="ADMIN", is_active=True)
        self.annotator_a = User(username="annotator_a", password_hash=hash_password("pass_a_1234"), role="ANNOTATOR", is_active=True)
        self.annotator_b = User(username="annotator_b", password_hash=hash_password("pass_b_1234"), role="ANNOTATOR", is_active=True)
        self.disabled_user = User(username="disabled_user", password_hash=hash_password("disabled_pass"), role="ANNOTATOR", is_active=False)
        self.db.add_all([self.admin, self.annotator_a, self.annotator_b, self.disabled_user])

        # Seed dataset and assignment for annotator A
        self.dataset = Dataset(name="sec_dataset", remote_name="nas", remote_path="sec_data", catalog_state="READY", current_catalog_version="v1")
        self.db.add(self.dataset)
        self.db.flush()

        self.file_a = DatasetFile(
            dataset_id=self.dataset.id,
            normalized_path="img_a.jpg",
            basename="img_a.jpg",
            extension="jpg",
            category="IMAGE",
            size=100,
            catalog_version="v1",
            file_state="ELIGIBLE",
        )
        self.db.add(self.file_a)
        self.db.flush()

        self.assignment_a = Assignment(
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
        self.db.add(self.assignment_a)
        self.db.flush()

        self.item_a = AssignmentItem(
            assignment_id=self.assignment_a.id,
            file_id=self.file_a.id,
            ordinal=1,
            item_state="PENDING",
        )
        self.db.add(self.item_a)
        self.db.commit()

        # Dependency override
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

    def test_annotator_b_cannot_access_annotator_a_manifest(self):
        token_b = create_access_token(data={"sub": self.annotator_b.username, "role": "ANNOTATOR", "uid": self.annotator_b.id})
        headers = {"Authorization": f"Bearer {token_b}"}

        # B attempts to access A's assignment manifest
        res = self.client.get(f"/api/v1/assignments/{self.assignment_a.id}/manifest", headers=headers)
        assert res.status_code == 403
        assert "Cannot access another user's assignment" in res.text

    def test_annotator_b_cannot_download_annotator_a_image(self):
        token_b = create_access_token(data={"sub": self.annotator_b.username, "role": "ANNOTATOR", "uid": self.annotator_b.id})
        headers = {"Authorization": f"Bearer {token_b}"}

        # B attempts to download A's assigned image
        res = self.client.get(f"/api/v1/assignments/{self.assignment_a.id}/images/{self.file_a.id}", headers=headers)
        assert res.status_code == 403

    def test_disabled_user_cannot_login_or_authenticate(self):
        # Disabled login attempt
        res = self.client.post("/api/v1/auth/login", json={"username": "disabled_user", "password": "disabled_pass"})
        assert res.status_code == 403
        assert "disabled" in res.text.lower()

        # Disabled token attempt
        token = create_access_token(data={"sub": "disabled_user", "role": "ANNOTATOR", "uid": self.disabled_user.id})
        headers = {"Authorization": f"Bearer {token}"}
        res2 = self.client.get("/api/v1/auth/me", headers=headers)
        assert res2.status_code == 401

    def test_invalid_token_rejected(self):
        headers = {"Authorization": "Bearer invalid_garbage_token"}
        res = self.client.get("/api/v1/auth/me", headers=headers)
        assert res.status_code == 401

    def test_secrets_redaction_in_config(self):
        d = settings.safe_dict()
        assert d["APP_SECRET_KEY"] == "******"
        assert d["POSTGRES_PASSWORD"] == "******"
        assert d["DATABASE_URL"] == "******"
