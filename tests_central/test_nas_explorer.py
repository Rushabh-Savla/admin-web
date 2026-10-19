import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from controller.app.database import Base, get_db
from controller.app.models import User
from controller.app.main import app
from controller.app.config import settings
from controller.app.auth import hash_password, create_access_token
from controller.app.nas_service import (
    obscure_password,
    sync_rclone_config_from_env,
    browse_nas_directories,
)


class TestNasExplorer:
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

        self.admin = User(username="admin_nas", password_hash=hash_password("admin_pass"), role="ADMIN", is_active=True)
        self.annotator = User(username="annotator_nas", password_hash=hash_password("ann_pass"), role="ANNOTATOR", is_active=True)
        self.db.add_all([self.admin, self.annotator])
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

    def test_sync_rclone_config_generation(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "test_rclone.conf"
        monkeypatch.setattr(settings, "RCLONE_CONFIG_PATH", str(cfg_file))
        monkeypatch.setattr(settings, "NAS_STORAGE_TYPE", "sftp")
        monkeypatch.setattr(settings, "NAS_HOST", "192.168.1.100")
        monkeypatch.setattr(settings, "NAS_PORT", 2222)
        monkeypatch.setattr(settings, "NAS_USER", "nasuser")
        monkeypatch.setattr(settings, "NAS_PASSWORD", "plainpass123")
        monkeypatch.setattr(settings, "DEFAULT_REMOTE_NAME", "annotation_nas")

        sync_rclone_config_from_env()

        assert cfg_file.exists()
        content = cfg_file.read_text()
        assert "[annotation_nas]" in content
        assert "type = sftp" in content
        assert "host = 192.168.1.100" in content
        assert "port = 2222" in content
        assert "user = nasuser" in content

    def test_browse_nas_directories_parsing(self):
        mock_lsjson_output = (
            '[{"Path": "camera_01", "Name": "camera_01", "IsDir": true, "Size": -1, "ModTime": "2026-10-14T10:00:00Z"},'
            '{"Path": "camera_02", "Name": "camera_02", "IsDir": true, "Size": -1, "ModTime": "2026-10-14T10:05:00Z"},'
            '{"Path": "sample.jpg", "Name": "sample.jpg", "IsDir": false, "Size": 1024, "ModTime": "2026-10-14T10:10:00Z"}]'
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_lsjson_output
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            res = browse_nas_directories(remote_name="annotation_nas", subpath="datasets/field")

        assert res["status"] == "OK"
        assert res["current_path"] == "datasets/field"
        assert res["parent_path"] == "datasets"
        assert len(res["directories"]) == 2
        assert res["directories"][0]["name"] == "camera_01"
        assert res["directories"][0]["relative_path"] == "datasets/field/camera_01"
        assert res["images_count"] == 1
        assert res["sample_images"][0]["name"] == "sample.jpg"

    def test_browse_api_rbac_enforcement(self):
        # 1. Annotator must be forbidden from NAS browsing
        r = self.client.get("/api/v1/nas/browse?path=", headers={"Authorization": f"Bearer {self.annotator_token}"})
        assert r.status_code == 403

        # 2. Admin should be allowed
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '[{"Path": "raw_data", "Name": "raw_data", "IsDir": true}]'
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            r = self.client.get("/api/v1/nas/browse?path=", headers={"Authorization": f"Bearer {self.admin_token}"})
            assert r.status_code == 200
            data = r.json()
            assert data["status"] == "OK"
            assert len(data["directories"]) == 1
            assert data["directories"][0]["name"] == "raw_data"

    def test_browse_absolute_sftp_path(self, monkeypatch):
        monkeypatch.setattr(settings, "NAS_ROOT_PATH", "/dataset")
        mock_lsjson_output = (
            '[{"Path": "cam_01", "Name": "cam_01", "IsDir": true, "Size": -1, "ModTime": "2026-10-14T12:00:00Z"},'
            '{"Path": "cam_02", "Name": "cam_02", "IsDir": true, "Size": -1, "ModTime": "2026-10-14T12:05:00Z"}]'
        )
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = mock_lsjson_output
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = browse_nas_directories(remote_name="annotation_nas", subpath="/dataset")
            # Verify target was passed with absolute slash
            args, kwargs = mock_run.call_args
            cmd_passed = args[0]
            assert "annotation_nas:/dataset" in cmd_passed

        assert res["status"] == "OK"
        assert res["current_path"] == "/dataset"
        assert res["parent_path"] == "/"
        assert len(res["directories"]) == 2
        assert res["directories"][0]["relative_path"] == "/dataset/cam_01"

    def test_nas_test_connection_endpoint(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "Total: 100GB\nUsed: 10GB"
        mock_proc.stderr = ""

        with patch("subprocess.run", return_value=mock_proc):
            r = self.client.post("/api/v1/nas/test", headers={"Authorization": f"Bearer {self.admin_token}"})
            assert r.status_code == 200
            data = r.json()
            assert data["connected"] is True
            assert data["remote"] == "annotation_nas"

    def test_dashboard_datasets_page_render(self):
        # Verify dashboard page loads without errors and includes default_root_path
        r = self.client.get("/dashboard/datasets", cookies={"nas_session": self.admin_token})
        assert r.status_code == 200
        assert "Live NAS Storage Explorer" in r.text
        assert "Register Dataset from NAS" in r.text
        assert "Select Current Folder" in r.text

