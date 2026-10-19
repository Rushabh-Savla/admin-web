import os
from unittest.mock import patch, MagicMock
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
    SyncJob,
    AuditEvent,
)
from controller.app.main import app
from controller.app.auth import hash_password, create_access_token


class TestUiRestructuring:
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

        # Seed Users
        self.admin = User(username="admin_ui", password_hash=hash_password("admin_pass"), role="ADMIN", is_active=True)
        self.annotator = User(username="annotator_ui", password_hash=hash_password("ann_pass"), role="ANNOTATOR", is_active=True)
        self.db.add_all([self.admin, self.annotator])
        self.db.commit()

        # Seed Project (Dataset)
        self.project = Dataset(
            title="Satellite Dataset",
            name="satellite_ds",
            remote_name="annotation_nas",
            remote_path="data/satellite",
            assigned_under="admin_ui",
            project_status="ACTIVE",
            catalog_state="READY",
        )
        self.db.add(self.project)
        self.db.commit()

        # Seed Dataset Files (10 images)
        self.files = []
        for i in range(10):
            f = DatasetFile(
                dataset_id=self.project.id,
                normalized_path=f"img_{i}.jpg",
                basename=f"img_{i}.jpg",
                extension="jpg",
                category="IMAGE",
                size=1024,
                catalog_version="v1",
                file_state="ELIGIBLE",
            )
            self.db.add(f)
            self.files.append(f)
        self.db.commit()

        # Seed Assignment with 2 completed items (20% progress)
        self.assignment = Assignment(
            dataset_id=self.project.id,
            annotator_id=self.annotator.id,
            creator_id=self.admin.id,
            directory_scope="",
            scope_type="RECURSIVE",
            requested_count=10,
            allocated_count=10,
            catalog_version="v1",
            status="IN_PROGRESS",
        )
        self.db.add(self.assignment)
        self.db.commit()

        for idx, f in enumerate(self.files):
            state = "VERIFIED_COMPLETE" if idx < 2 else ("BACKGROUND_REVIEW" if idx == 2 else "PENDING")
            item = AssignmentItem(
                assignment_id=self.assignment.id,
                file_id=f.id,
                ordinal=idx,
                item_state=state,
            )
            self.db.add(item)
        self.db.commit()

        # Seed SyncJob and AuditEvent
        self.job = SyncJob(
            job_type="CATALOG_SCAN",
            status="SUCCESS",
            payload={"dataset_id": self.project.id},
            claimed_worker="worker-1",
        )
        self.audit = AuditEvent(
            user_id=self.admin.id,
            action="PROJECT_CREATED",
            resource_type="dataset",
            resource_id=str(self.project.id),
            details={"title": self.project.title},
        )
        self.db.add_all([self.job, self.audit])
        self.db.commit()

        def override_get_db():
            try:
                yield self.db
            finally:
                pass

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        self.admin_token = create_access_token(data={"sub": self.admin.username, "role": self.admin.role, "uid": self.admin.id})
        self.annotator_token = create_access_token(data={"sub": self.annotator.username, "role": self.annotator.role, "uid": self.annotator.id})

        yield

        app.dependency_overrides.clear()
        self.db.close()

    def test_target_navigation_structure(self):
        """
        Verify Acceptance Criteria:
        1. Top nav contains Projects, NAS Explorer, Users & Devices, System Activity.
        2. Top nav does NOT contain separate Assignments, Review Queue, Sync Jobs, Audit Log.
        3. Annotator only sees Projects, NAS Explorer (no Users & Devices or System Activity).
        """
        # Admin view
        res = self.client.get("/dashboard", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        # Required top navigation entries
        assert '<a href="/dashboard" class="active">Projects</a>' in html or 'href="/dashboard"' in html
        assert 'href="/dashboard/datasets"' in html
        assert 'href="/dashboard/users"' in html
        assert 'href="/dashboard/activity"' in html

        # Removed standalone top navigation items
        assert '<a href="/dashboard/assignments">Assignments</a>' not in html
        assert '<a href="/dashboard/reviews">Review Queue</a>' not in html
        assert '<a href="/dashboard/jobs">Sync Jobs</a>' not in html
        assert '<a href="/dashboard/audit">Audit Log</a>' not in html

        # Annotator view
        res_ann = self.client.get("/dashboard", cookies={"nas_session": self.annotator_token})
        assert res_ann.status_code == 200
        html_ann = res_ann.text
        assert 'href="/dashboard/users"' not in html_ann
        assert 'href="/dashboard/activity"' not in html_ann

    def test_projects_dashboard_collapsed_by_default(self):
        """
        Verify Acceptance Criteria:
        - Every project card starts collapsed on fresh page load.
        - Compact header shows only project name, progress bar, percentage, and expand chevron.
        - Detailed information (tables, badges, paths) is hidden inside collapsed container.
        """
        res = self.client.get("/dashboard", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        # Verify compact header elements
        assert f'id="proj-header-{self.project.id}"' in html
        assert 'class="project-compact-header"' in html
        assert 'Satellite Dataset' in html
        assert 'class="project-compact-pct"' in html
        assert '20%' in html  # 2 of 10 items completed
        assert f'id="chevron-{self.project.id}"' in html

        # Verify expanded body is initially collapsed with display: none
        assert f'id="proj-body-{self.project.id}" style="display: none;"' in html

        # Verify subnav tabs inside Projects
        assert 'class="subnav-tab active"' in html
        assert 'Projects Overview' in html
        assert 'All Assignments' in html
        assert 'Review Queue' in html

    def test_project_detail_structure_and_overview_collapsible_sections(self):
        """
        Verify Acceptance Criteria:
        - Project detail contains Overview, Assignments, Review Queue tabs.
        - Overview sections are collapsible and collapsed by default.
        """
        # Overview tab (default)
        res = self.client.get(f"/dashboard/projects/{self.project.id}", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        assert f'href="/dashboard/projects/{self.project.id}?tab=overview"' in html
        assert f'href="/dashboard/projects/{self.project.id}?tab=assignments"' in html
        assert f'href="/dashboard/projects/{self.project.id}?tab=reviews"' in html

        # Verify collapsible overview sections are collapsed by default (style="display: none;")
        assert 'id="collapsible-body-info" style="display: none;"' in html
        assert 'id="collapsible-body-dirs" style="display: none;"' in html
        assert 'id="collapsible-body-assigns" style="display: none;"' in html

    def test_project_detail_assignments_and_wizard_preselection(self):
        """
        Verify Acceptance Criteria:
        - Assignments tab displays assignments for this project.
        - Assignment wizard preselects and locks this project.
        """
        res = self.client.get(f"/dashboard/projects/{self.project.id}?tab=assignments", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        # Verify project-specific assignments table
        assert f'Assignments Belonging to "Satellite Dataset"' in html
        assert f'#{self.assignment.id}' in html

        # Verify wizard preselects this dataset ID
        assert f'name="dataset_id" id="wizard_dataset_id" value="{self.project.id}"' in html

    def test_project_detail_review_queue(self):
        """
        Verify Acceptance Criteria:
        - Review Queue tab shows pending reviews belonging to this project.
        """
        res = self.client.get(f"/dashboard/projects/{self.project.id}?tab=reviews", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        assert f'Review Queue for "Satellite Dataset"' in html
        assert 'BACKGROUND_REVIEW' in html
        assert 'Approve' in html
        assert 'Reject' in html

    def test_system_activity_consolidation_and_legacy_redirects(self):
        """
        Verify Acceptance Criteria:
        - System Activity combines Sync Jobs and Audit Log chronologically.
        - Subtabs for All Activity, Sync Jobs, and Audit Trail.
        - Legacy routes /dashboard/jobs and /dashboard/audit redirect (302) to /dashboard/activity.
        """
        # System Activity page
        res = self.client.get("/dashboard/activity", cookies={"nas_session": self.admin_token})
        assert res.status_code == 200
        html = res.text

        assert 'System Activity' in html
        assert 'Chronological Activity Feed' in html
        assert 'Sync Job' in html
        assert 'CATALOG_SCAN' in html
        assert 'Audit Event' in html
        assert 'PROJECT_CREATED' in html

        # Legacy redirects
        res_jobs = self.client.get("/dashboard/jobs", cookies={"nas_session": self.admin_token}, follow_redirects=False)
        assert res_jobs.status_code == 302
        assert res_jobs.headers["location"] == "/dashboard/activity?tab=jobs"

        res_audit = self.client.get("/dashboard/audit", cookies={"nas_session": self.admin_token}, follow_redirects=False)
        assert res_audit.status_code == 302
        assert res_audit.headers["location"] == "/dashboard/activity?tab=audit"

    def test_all_assignments_and_review_queue_pages_under_projects(self):
        """
        Verify Acceptance Criteria:
        - /dashboard/assignments and /dashboard/reviews remain accessible under Projects navigation.
        - Review queue supports project filtering.
        """
        # Assignments
        res_a = self.client.get("/dashboard/assignments", cookies={"nas_session": self.admin_token})
        assert res_a.status_code == 200
        assert 'Image Assignments (All Projects)' in res_a.text
        assert 'Projects Overview' in res_a.text

        # Reviews with project filter
        res_r = self.client.get("/dashboard/reviews", cookies={"nas_session": self.admin_token})
        assert res_r.status_code == 200
        assert 'Filter by Project:' in res_r.text
        assert 'Satellite Dataset' in res_r.text
