import os
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
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
    AnnotationSubmission,
    BackgroundDeclaration,
)
from controller.app.main import app
from controller.app.auth import hash_password
from controller.app.assignment_service import create_atomic_assignment
try:
    from annotator_agent.agent import AnnotatorAgent
except ImportError:
    AnnotatorAgent = None
from sync_worker.staging import prepare_assignment_workspace
from sync_worker.transfer import upload_and_verify_annotation


class TestAgentLifecycle:
    @pytest.fixture(autouse=True)
    def setup_e2e(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mock_nas = self.tmp / "nas"
        self.mock_nas.mkdir(parents=True)
        self.client_workspace = self.tmp / "local_agent_ws"
        self.client_workspace.mkdir(parents=True)
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
        self.admin = User(username="admin_e2e", password_hash=hash_password("admin_pass123"), role="ADMIN", is_active=True)
        self.annotator = User(username="ann_e2e", password_hash=hash_password("ann_pass123"), role="ANNOTATOR", is_active=True)
        self.db.add_all([self.admin, self.annotator])

        # Seed dataset
        self.dataset = Dataset(
            name="lifecycle_ds",
            remote_name="nas",
            remote_path="ds_data",
            catalog_state="READY",
            current_catalog_version="cat_v1",
        )
        self.db.add(self.dataset)
        self.db.flush()

        # Seed 2 image files
        self.files = []
        for i in range(1, 3):
            rel = f"batch1/image_{i:03d}.jpg"
            img_data = f"IMAGE_CONTENT_{i}".encode("utf-8")
            disk_file = self.mock_nas / "ds_data" / rel
            disk_file.parent.mkdir(parents=True, exist_ok=True)
            disk_file.write_bytes(img_data)

            f = DatasetFile(
                dataset_id=self.dataset.id,
                normalized_path=rel,
                basename=f"image_{i:03d}.jpg",
                extension="jpg",
                category="IMAGE",
                size=len(img_data),
                fingerprint=hashlib.sha256(img_data).hexdigest()[:16],
                catalog_version="cat_v1",
                file_state="ELIGIBLE",
            )
            self.db.add(f)
            self.files.append(f)
        self.db.commit()

        def override_get_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        self.test_client = TestClient(app)

        from controller.app.auth import create_access_token
        self.admin_token = create_access_token(data={"sub": self.admin.username, "role": self.admin.role, "uid": self.admin.id})
        self.annotator_token = create_access_token(data={"sub": self.annotator.username, "role": self.annotator.role, "uid": self.annotator.id})

        yield

        app.dependency_overrides.clear()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @pytest.mark.skipif(AnnotatorAgent is None, reason="annotator_agent not available in standalone web repo")
    def test_full_agent_annotation_sync_lifecycle(self):
        # 1. Manager creates atomic assignment of 2 images to annotator
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch1",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=2,
        )
        assert assignment.allocated_count == 2
        assert assignment.workspace_path == f"ds_data/ann_e2e/assignment_{assignment.id}"
        prepare_assignment_workspace(self.db, assignment.id, mock_base_dir=self.mock_nas)

        # Verify NAS subdirectory with annotator username was created
        staged_img = self.mock_nas / "ds_data" / "ann_e2e" / f"assignment_{assignment.id}" / "images" / "batch1" / "image_001.jpg"
        assert staged_img.exists()

        # 2. Initialize Annotator Agent
        agent = AnnotatorAgent(
            server_url="http://testserver",
            local_root=str(self.client_workspace),
            batch_size=10,
        )
        # Point agent's HTTP client to TestClient
        agent.client._client = self.test_client

        # Login
        agent.login("ann_e2e", "ann_pass123")
        assert agent.client.token is not None

        # 3. Agent discovers active assignment
        active = agent.get_active_assignment()
        assert active is not None
        assert active["id"] == assignment.id

        # 4. Agent downloads batch images
        downloaded = agent.fetch_batch(active["id"])
        assert downloaded == 2

        # Verify files exist in local agent workspace
        local_img1 = self.client_workspace / "current" / "batch1" / "image_001.jpg"
        local_img2 = self.client_workspace / "current" / "batch1" / "image_002.jpg"
        assert local_img1.exists()
        assert local_img2.exists()

        # 5. Simulate XAnyLabeling creating annotations locally
        local_ann1 = self.client_workspace / "current" / "batch1" / "image_001.json"
        local_ann2 = self.client_workspace / "current" / "batch1" / "image_002.json"

        ann_data = {
            "version": "1.0",
            "shapes": [{"label": "equipment", "points": [[10, 20], [30, 40]], "shape_type": "rectangle"}],
        }
        local_ann1.write_text(json.dumps(ann_data), encoding="utf-8")
        local_ann2.write_text(json.dumps(ann_data), encoding="utf-8")

        # 6. Agent syncs annotations to server
        res = agent.sync_annotations(active["id"], active["lease_generation"])
        assert res["submitted"] == 2
        assert res["errors"] == 0

        # Verify local backup was made
        backup_1 = self.client_workspace / "backups" / "batch1" / "image_001.json"
        assert backup_1.exists()

        # 7. Worker executes upload & verify
        submissions = self.db.query(AnnotationSubmission).filter(AnnotationSubmission.assignment_id == assignment.id).all()
        assert len(submissions) == 2
        for sub in submissions:
            upload_and_verify_annotation(self.db, sub.id, mock_base_dir=self.mock_nas)

        # 8. Check that assignment entered SUBMITTED_FOR_REVIEW awaiting master approval
        self.db.refresh(assignment)
        assert assignment.status == "SUBMITTED_FOR_REVIEW"

        # 9. Master approves completion via API
        resp = self.test_client.post(
            f"/api/v1/assignments/{assignment.id}/approve-completion",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        assert resp.status_code == 200
        self.db.refresh(assignment)
        assert assignment.status == "COMPLETED"
        claims = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims == 0

    @pytest.mark.skipif(AnnotatorAgent is None, reason="annotator_agent not available in standalone web repo")
    def test_e2e_partial_annotations_with_bulk_background(self):
        # 1. Create assignment with 2 images
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch1",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=2,
            idempotency_key="idemp_bg_test_1",
            allow_partial=True,
        )
        self.db.commit()
        prepare_assignment_workspace(self.db, assignment.id, mock_base_dir=self.mock_nas)
        self.db.refresh(assignment)

        agent = AnnotatorAgent(
            server_url="http://testserver",
            local_root=str(self.client_workspace),
        )
        agent.client._client = self.test_client
        agent.login("ann_e2e", "ann_pass123")

        # Fetch batch
        downloaded = agent.fetch_batch(assignment.id)
        assert downloaded == 2

        # Annotate ONLY 1 image out of 2 (e.g. image_001 has target object, image_002 is background)
        local_ann1 = self.client_workspace / "current" / "batch1" / "image_001.json"
        ann_data = {
            "version": "1.0",
            "shapes": [{"label": "equipment", "points": [[10, 20], [30, 40]], "shape_type": "rectangle"}],
        }
        local_ann1.write_text(json.dumps(ann_data), encoding="utf-8")

        # Sync with declare_remaining_as_background=True
        res = agent.sync_annotations(
            assignment_id=assignment.id,
            ownership_generation=assignment.lease_generation,
            declare_remaining_as_background=True,
            background_reason="Negative / Background sample (no target objects)",
        )
        assert res["submitted"] == 1
        assert res["background"] == 1
        assert res["errors"] == 0
        assert res["batch_completed"] is True

        # Process upload for the 1 submitted annotation
        submissions = self.db.query(AnnotationSubmission).filter(AnnotationSubmission.assignment_id == assignment.id).all()
        assert len(submissions) == 1
        upload_and_verify_annotation(self.db, submissions[0].id, mock_base_dir=self.mock_nas)

        # Assignment enters SUBMITTED_FOR_REVIEW because all items are either verified or background review
        self.db.refresh(assignment)
        assert assignment.status == "SUBMITTED_FOR_REVIEW"

        # Master approves completion
        resp = self.test_client.post(
            f"/api/v1/assignments/{assignment.id}/approve-completion",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        assert resp.status_code == 200
        self.db.refresh(assignment)
        assert assignment.status == "COMPLETED"
        claims = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims == 0

    def test_master_completion_deny_and_notifications_workflow(self):
        # 1. Create an assignment
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch1",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=1,
            idempotency_key="idemp_deny_test_1",
            allow_partial=True,
        )
        self.db.commit()

        # 2. Annotator manually submits completion
        sub_resp = self.test_client.post(
            f"/api/v1/assignments/{assignment.id}/submit-completion",
            headers={"Authorization": f"Bearer {self.annotator_token}"},
        )
        assert sub_resp.status_code == 200
        self.db.refresh(assignment)
        assert assignment.status == "SUBMITTED_FOR_REVIEW"

        # 3. Check notifications endpoint returns this pending approval
        notif_resp = self.test_client.get(
            "/api/v1/notifications",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        assert notif_resp.status_code == 200
        notif_data = notif_resp.json()
        assert notif_data["total_count"] >= 1
        comp_ids = [c["assignment_id"] for c in notif_data["pending_completions"]]
        assert assignment.id in comp_ids

        # 4. Master denies completion with feedback
        deny_resp = self.test_client.post(
            f"/api/v1/assignments/{assignment.id}/deny-completion?reason=Bounding+boxes+need+tightening",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        assert deny_resp.status_code == 200
        self.db.refresh(assignment)
        assert assignment.status == "IN_PROGRESS"

        # 5. Active claims remain retained while In Progress
        claims = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims == 1

        # 6. Master approves completion
        appr_resp = self.test_client.post(
            f"/api/v1/assignments/{assignment.id}/approve-completion",
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        assert appr_resp.status_code == 200
        self.db.refresh(assignment)
        assert assignment.status == "COMPLETED"
        claims_after = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims_after == 0

    def test_operations_and_dashboard_pages(self):
        # Operations page renders with jobs and audit
        r_ops = self.test_client.get("/dashboard/operations", cookies={"nas_session": self.admin_token})
        assert r_ops.status_code == 200
        assert "System Operations &amp; Audit Trail" in r_ops.text or "System Operations & Audit Trail" in r_ops.text
        assert "Sync Jobs Queue" in r_ops.text
        assert "Audit Trail" in r_ops.text

        # /dashboard/jobs redirects to operations
        r_jobs = self.test_client.get("/dashboard/jobs", cookies={"nas_session": self.admin_token}, follow_redirects=False)
        assert r_jobs.status_code == 302
        assert "/dashboard/operations?tab=jobs" in r_jobs.headers["location"]

        # /dashboard/audit redirects to operations
        r_audit = self.test_client.get("/dashboard/audit", cookies={"nas_session": self.admin_token}, follow_redirects=False)
        assert r_audit.status_code == 302
        assert "/dashboard/operations?tab=audit" in r_audit.headers["location"]

        # Dashboard overview renders with collapsed project view and notifications bell
        r_dash = self.test_client.get("/dashboard", cookies={"nas_session": self.admin_token})
        assert r_dash.status_code == 200
        assert "notif-bell-btn" in r_dash.text
        assert "project-collapsed-row" in r_dash.text
        assert "Review Queue" in r_dash.text

    def test_assignment_level_approval_dashboard_and_html(self):
        # 1. Create assignment with 2 images
        assignment = create_atomic_assignment(
            db=self.db,
            dataset_id=self.dataset.id,
            directory_scope="batch1",
            scope_type="RECURSIVE",
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            requested_count=2,
            idempotency_key="idemp_dash_appr_1",
            allow_partial=True,
        )
        self.db.commit()

        # Mark 1 item as background declared and 1 item as verified complete
        items = self.db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).all()
        assert len(items) == 2
        items[0].item_state = "VERIFIED_COMPLETE"
        items[1].item_state = "BACKGROUND_DECLARED"
        bg_decl = BackgroundDeclaration(
            assignment_id=assignment.id,
            file_id=items[1].file_id,
            annotator_id=self.annotator.id,
            reason="No visible target objects",
            approval_state="PENDING_ASSIGNMENT_APPROVAL",
        )
        self.db.add(bg_decl)
        assignment.status = "SUBMITTED_FOR_REVIEW"
        self.db.commit()

        # 2. Review Queue tab displays the complete assignment awaiting approval
        r_rev = self.test_client.get("/dashboard?tab=reviews", cookies={"nas_session": self.admin_token})
        assert r_rev.status_code == 200
        assert f"#{assignment.id}" in r_rev.text
        assert "Assignments Awaiting Master Review &amp; Completion Approval" in r_rev.text or "Assignments Awaiting Master Review & Completion Approval" in r_rev.text
        assert "1 objects" in r_rev.text
        assert "1 background" in r_rev.text
        assert "Approve Assignment" in r_rev.text

        # 3. Assignment detail page shows completion approval action banner
        r_detail = self.test_client.get(f"/dashboard/assignments/{assignment.id}", cookies={"nas_session": self.admin_token})
        assert r_detail.status_code == 200
        assert "Assignment Submitted for Master Completion Approval" in r_detail.text
        assert "Approve Complete Assignment" in r_detail.text

        # 4. Deny assignment via dashboard POST
        r_deny = self.test_client.post(
            f"/dashboard/assignments/{assignment.id}/deny",
            data={"reason": "Please check image #2 again"},
            cookies={"nas_session": self.admin_token},
            follow_redirects=False,
        )
        assert r_deny.status_code == 302
        self.db.refresh(assignment)
        assert assignment.status == "IN_PROGRESS"
        self.db.refresh(bg_decl)
        assert bg_decl.approval_state == "REJECTED"
        self.db.refresh(items[1])
        assert items[1].item_state == "ANNOTATING"

        # 5. Re-submit and Approve assignment via dashboard POST
        assignment.status = "SUBMITTED_FOR_REVIEW"
        self.db.commit()

        r_appr = self.test_client.post(
            f"/dashboard/assignments/{assignment.id}/approve",
            cookies={"nas_session": self.admin_token},
            follow_redirects=False,
        )
        assert r_appr.status_code == 302
        self.db.refresh(assignment)
        assert assignment.status == "COMPLETED"
        self.db.refresh(bg_decl)
        assert bg_decl.approval_state == "APPROVED"
        for itm in self.db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).all():
            assert itm.item_state == "VERIFIED_COMPLETE"
        claims = self.db.query(ActiveImageClaim).filter(ActiveImageClaim.assignment_id == assignment.id).count()
        assert claims == 0



