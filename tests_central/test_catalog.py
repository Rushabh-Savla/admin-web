import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import pytest
from sync_worker.catalog_adapter import (
    LocalMockCatalogAdapter,
    build_directory_tree_counts,
    calculate_catalog_version,
    classify_file,
    sanitize_relative_path,
)

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


class TestCatalogAdapter:
    @pytest.fixture(autouse=True)
    def setup_test_dir(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dataset_dir = self.tmp / "dataset"
        self.dataset_dir.mkdir(parents=True)
        yield
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_case_insensitive_format_classification(self):
        test_cases = [
            ("image1.JPG", ("jpg", "IMAGE")),
            ("image2.Png", ("png", "IMAGE")),
            ("image3.JPEG", ("jpeg", "IMAGE")),
            ("image4.WebP", ("webp", "IMAGE")),
            ("image5.TIFF", ("tiff", "IMAGE")),
            ("label1.JSON", ("json", "JSON")),
            ("notes.Txt", ("txt", "TXT")),
            ("meta.XML", ("xml", "XML")),
            ("archive.tar.gz", ("gz", "OTHER")),
            ("no_extension", ("", "OTHER")),
        ]
        for filename, (expected_ext, expected_cat) in test_cases:
            ext, cat = classify_file(filename, SUPPORTED_EXTS)
            assert ext == expected_ext
            assert cat == expected_cat

    def test_path_sanitization_and_traversal_prevention(self):
        valid = [
            ("folder/image.jpg", "folder/image.jpg"),
            ("a/b/c/d.png", "a/b/c/d.png"),
            ("./folder//file.jpg", "folder/file.jpg"),
            ("windows\\style\\path.jpg", "windows/style/path.jpg"),
        ]
        for inp, expected in valid:
            assert sanitize_relative_path(inp) == expected

        invalid = [
            "../etc/passwd",
            "folder/../../escape.jpg",
            "/absolute/path/file.jpg",
            "C:\\windows\\system32\\cmd.exe",
            "dir/\0nullbyte.jpg",
        ]
        for bad in invalid:
            with pytest.raises(ValueError):
                sanitize_relative_path(bad)

    def test_duplicate_names_in_different_directories(self):
        (self.dataset_dir / "dirA").mkdir()
        (self.dataset_dir / "dirB").mkdir()
        (self.dataset_dir / "dirA" / "image.jpg").write_bytes(b"data1")
        (self.dataset_dir / "dirB" / "image.jpg").write_bytes(b"data2")

        adapter = LocalMockCatalogAdapter(self.tmp)
        entries = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, []))

        paths = [e.normalized_path for e in entries]
        assert len(paths) == 2
        assert "dirA/image.jpg" in paths
        assert "dirB/image.jpg" in paths
        assert len(set(paths)) == 2

    def test_recursive_and_direct_directory_counts(self):
        # Create structure:
        # dataset/
        #   img_root.png
        #   root.json
        #   folder1/
        #     img1.jpg
        #     img2.jpg
        #     folder2/
        #       img3.jpg
        #       info.txt
        (self.dataset_dir / "img_root.png").write_bytes(b"1")
        (self.dataset_dir / "root.json").write_bytes(b"{}")
        (self.dataset_dir / "folder1").mkdir()
        (self.dataset_dir / "folder1" / "img1.jpg").write_bytes(b"2")
        (self.dataset_dir / "folder1" / "img2.jpg").write_bytes(b"3")
        (self.dataset_dir / "folder1" / "folder2").mkdir()
        (self.dataset_dir / "folder1" / "folder2" / "img3.jpg").write_bytes(b"4")
        (self.dataset_dir / "folder1" / "folder2" / "info.txt").write_bytes(b"txt")

        adapter = LocalMockCatalogAdapter(self.tmp)
        entries = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, []))
        tree = build_directory_tree_counts(entries)

        # Root directory ""
        root_dir = tree[""]
        assert root_dir.direct_image_count == 1
        assert root_dir.direct_json_count == 1
        assert root_dir.recursive_image_count == 4  # 1 root + 2 folder1 + 1 folder2
        assert root_dir.recursive_json_count == 1
        assert root_dir.recursive_txt_count == 1

        # folder1
        f1 = tree["folder1"]
        assert f1.direct_image_count == 2
        assert f1.recursive_image_count == 3  # 2 direct + 1 folder2

        # folder2
        f2 = tree["folder1/folder2"]
        assert f2.direct_image_count == 1
        assert f2.recursive_image_count == 1
        assert f2.direct_txt_count == 1

    def test_exclusion_patterns(self):
        (self.dataset_dir / ".Trash").mkdir()
        (self.dataset_dir / ".Trash" / "deleted.jpg").write_bytes(b"del")
        (self.dataset_dir / ".DS_Store").write_bytes(b"ds")
        (self.dataset_dir / "valid.jpg").write_bytes(b"ok")

        exclusions = [".Trash/**", "**/.DS_Store"]
        adapter = LocalMockCatalogAdapter(self.tmp)
        entries = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, exclusions))

        paths = [e.normalized_path for e in entries]
        assert paths == ["valid.jpg"]

    def test_catalog_version_and_refresh_changes(self):
        f1 = self.dataset_dir / "img1.jpg"
        f1.write_bytes(b"content1")
        adapter = LocalMockCatalogAdapter(self.tmp)

        entries_v1 = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, []))
        v1 = calculate_catalog_version(entries_v1)

        # Add file
        f2 = self.dataset_dir / "img2.jpg"
        f2.write_bytes(b"content2")
        entries_v2 = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, []))
        v2 = calculate_catalog_version(entries_v2)

        assert v1 != v2

        # Remove file
        f2.unlink()
        entries_v3 = list(adapter.scan_files("mock", "dataset", SUPPORTED_EXTS, []))
        v3 = calculate_catalog_version(entries_v3)

        assert v3 == v1
