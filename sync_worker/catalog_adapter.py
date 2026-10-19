import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Generator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    normalized_path: str
    basename: str
    extension: str
    category: str  # IMAGE, JSON, TXT, XML, OTHER
    size: int
    remote_mtime: Optional[datetime]
    fingerprint: Optional[str] = None


@dataclass
class DirectoryCounts:
    normalized_path: str
    parent_path: Optional[str]
    direct_image_count: int = 0
    direct_json_count: int = 0
    direct_txt_count: int = 0
    direct_other_count: int = 0
    recursive_image_count: int = 0
    recursive_json_count: int = 0
    recursive_txt_count: int = 0
    recursive_other_count: int = 0


def sanitize_relative_path(path: str) -> str:
    """
    Strict path sanitization to prevent directory traversal and remote injection.
    Rejects '..', absolute paths, backslashes, drive letters, and null bytes.
    """
    if not path or "\0" in path:
        raise ValueError("Invalid path: contains null bytes or is empty")

    raw_stripped = path.strip()
    if raw_stripped.startswith("/") or raw_stripped.startswith("\\"):
        raise ValueError(f"Absolute paths not allowed in '{path}'")

    norm = raw_stripped.replace("\\", "/")
    parts = norm.split("/")
    clean_parts = []
    for part in parts:
        part = part.strip()
        if not part or part == ".":
            continue
        if part == "..":
            raise ValueError(f"Path traversal detected in '{path}'")
        if ":" in part:
            raise ValueError(f"Drive letter or invalid character in path segment '{part}'")
        clean_parts.append(part)

    clean_path = "/".join(clean_parts)
    if not clean_path:
        raise ValueError("Path evaluates to empty after normalization")
    return clean_path


def classify_file(filename: str, image_extensions: Set[str]) -> Tuple[str, str]:
    """Classify file into category based on extension (case-insensitive)."""
    norm_name = filename.strip()
    dot_idx = norm_name.rfind(".")
    if dot_idx == -1 or dot_idx == len(norm_name) - 1:
        return "", "OTHER"

    ext = norm_name[dot_idx:].lower()
    raw_ext = ext.lstrip(".")

    if ext in image_extensions:
        return raw_ext, "IMAGE"
    elif ext == ".json":
        return raw_ext, "JSON"
    elif ext == ".txt":
        return raw_ext, "TXT"
    elif ext in {".xml", ".yaml", ".yml", ".csv"}:
        return raw_ext, "XML"
    else:
        return raw_ext, "OTHER"


class BaseCatalogAdapter:
    def scan_files(
        self,
        remote_name: str,
        remote_path: str,
        image_extensions: Set[str],
        exclusions: List[str],
    ) -> Generator[CatalogEntry, None, None]:
        raise NotImplementedError


class LocalMockCatalogAdapter(BaseCatalogAdapter):
    """Catalog adapter for local directory testing and mock NAS mode."""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def scan_files(
        self,
        remote_name: str,
        remote_path: str,
        image_extensions: Set[str],
        exclusions: List[str],
    ) -> Generator[CatalogEntry, None, None]:
        target_root = (self.base_dir / remote_path.lstrip("/")).resolve()
        if not target_root.exists() or not target_root.is_dir():
            logger.warning(f"Mock root does not exist: {target_root}")
            return

        for p in sorted(target_root.rglob("*")):
            if not p.is_file():
                continue

            rel_path = p.relative_to(target_root).as_posix()
            try:
                clean_path = sanitize_relative_path(rel_path)
            except ValueError:
                continue

            path_segments = clean_path.split("/")
            if any(seg.startswith("assignment_") for seg in path_segments[:-1]):
                continue

            # Check exclusions
            excluded = False
            for exc in exclusions:
                if exc.endswith("/**"):
                    prefix = exc[:-3].rstrip("/")
                    if clean_path.startswith(prefix + "/") or clean_path == prefix:
                        excluded = True
                        break
                elif exc.startswith("**/"):
                    suffix = exc[3:]
                    if clean_path.endswith("/" + suffix) or clean_path == suffix or ("/" + suffix + "/") in ("/" + clean_path + "/"):
                        excluded = True
                        break
                elif clean_path == exc or p.name == exc:
                    excluded = True
                    break
            if excluded:
                continue

            stat = p.stat()
            ext, category = classify_file(p.name, image_extensions)
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            # Fast fingerprint based on path, size, and mtime
            fp_src = f"{clean_path}:{stat.st_size}:{int(stat.st_mtime)}"
            fp = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]

            yield CatalogEntry(
                normalized_path=clean_path,
                basename=p.name,
                extension=ext,
                category=category,
                size=stat.st_size,
                remote_mtime=mtime,
                fingerprint=fp,
            )


class RcloneCatalogAdapter(BaseCatalogAdapter):
    """Catalog adapter querying real NAS via Rclone lsjson."""

    def __init__(self, rclone_bin: str = "rclone", config_path: Optional[str] = None):
        self.rclone_bin = rclone_bin
        self.config_path = config_path

    def scan_files(
        self,
        remote_name: str,
        remote_path: str,
        image_extensions: Set[str],
        exclusions: List[str],
    ) -> Generator[CatalogEntry, None, None]:
        clean_remote_path = remote_path.strip()
        if clean_remote_path.startswith("/"):
            target = f"{remote_name}:{clean_remote_path}"
        elif clean_remote_path:
            target = f"{remote_name}:{clean_remote_path}"
        else:
            target = f"{remote_name}:"

        cmd = [
            self.rclone_bin,
            "lsjson",
            target,
            "--recursive",
            "--files-only",
            "--no-mimetype",
            "--timeout",
            "60s",
            "--contimeout",
            "10s",
        ]
        if self.config_path and Path(self.config_path).exists() and Path(self.config_path).stat().st_size > 0:
            cmd += ["--config", self.config_path]
        always_excludes = [
            ".Trash*/**",
            "**/.Trash*/**",
            "#Recycle/**",
            "**/#Recycle/**",
            "@eaDir/**",
            "**/@eaDir/**",
            "assignment_*/**",
            "**/assignment_*/**",
        ]
        for exc in always_excludes:
            cmd += ["--exclude", exc]

        for exc in exclusions:
            if exc.strip():
                cmd += ["--exclude", exc.strip()]

        cmd.append("--ignore-errors")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        stdout, stderr = proc.communicate(timeout=3600)
        if proc.returncode != 0 and not stdout.strip():
            raise RuntimeError(f"Rclone listing failed (code {proc.returncode}): {stderr[:500]}")

        try:
            items = json.loads(stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Failed to decode Rclone lsjson output: {exc}")

        for item in items:
            raw_path = item.get("Path") or item.get("Name", "")
            try:
                clean_path = sanitize_relative_path(raw_path)
            except ValueError:
                continue

            path_segments = clean_path.split("/")
            if any(seg.startswith("assignment_") for seg in path_segments[:-1]):
                continue

            size = int(item.get("Size", 0))
            mtime_str = item.get("ModTime")
            mtime = None
            if mtime_str:
                try:
                    mtime = datetime.fromisoformat(mtime_str.replace("Z", "+00:00"))
                except Exception:
                    pass

            name = item.get("Name") or Path(clean_path).name
            ext, category = classify_file(name, image_extensions)

            hashes = item.get("Hashes") or {}
            fp = hashes.get("SHA-256") or hashes.get("SHA-1") or hashes.get("MD5")
            if not fp and mtime:
                fp_src = f"{clean_path}:{size}:{int(mtime.timestamp())}"
                fp = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]

            yield CatalogEntry(
                normalized_path=clean_path,
                basename=name,
                extension=ext,
                category=category,
                size=size,
                remote_mtime=mtime,
                fingerprint=fp,
            )


def build_directory_tree_counts(entries: List[CatalogEntry]) -> Dict[str, DirectoryCounts]:
    """
    Computes direct and recursive counts for all directories in the tree.
    Root directory is represented by "" (empty string).
    """
    dirs: Dict[str, DirectoryCounts] = {
        "": DirectoryCounts(normalized_path="", parent_path=None)
    }

    # First pass: identify all directories and calculate direct counts
    for entry in entries:
        parts = entry.normalized_path.split("/")
        if len(parts) == 1:
            dir_path = ""
        else:
            dir_path = "/".join(parts[:-1])

        # Ensure directory and all its parent directories exist in the map
        curr = dir_path
        while True:
            if curr not in dirs:
                if "/" in curr:
                    parent = curr.rsplit("/", 1)[0]
                else:
                    parent = "" if curr != "" else None
                dirs[curr] = DirectoryCounts(normalized_path=curr, parent_path=parent)
            if curr == "":
                break
            curr = "" if "/" not in curr else curr.rsplit("/", 1)[0]

        target_dir = dirs[dir_path]
        if entry.category == "IMAGE":
            target_dir.direct_image_count += 1
        elif entry.category == "JSON":
            target_dir.direct_json_count += 1
        elif entry.category == "TXT":
            target_dir.direct_txt_count += 1
        else:
            target_dir.direct_other_count += 1

    # Second pass: calculate recursive counts from bottom up
    sorted_paths = sorted(dirs.keys(), key=lambda p: p.count("/"), reverse=True)
    for p in sorted_paths:
        d = dirs[p]
        d.recursive_image_count += d.direct_image_count
        d.recursive_json_count += d.direct_json_count
        d.recursive_txt_count += d.direct_txt_count
        d.recursive_other_count += d.direct_other_count

        if d.parent_path is not None and d.parent_path in dirs:
            parent = dirs[d.parent_path]
            parent.recursive_image_count += d.recursive_image_count
            parent.recursive_json_count += d.recursive_json_count
            parent.recursive_txt_count += d.recursive_txt_count
            parent.recursive_other_count += d.recursive_other_count

    return dirs


def calculate_catalog_version(entries: List[CatalogEntry]) -> str:
    """Calculate an immutable version hash for a catalog snapshot."""
    h = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.normalized_path):
        mtime_epoch = int(entry.remote_mtime.timestamp()) if entry.remote_mtime else 0
        h.update(f"{entry.normalized_path}|{entry.size}|{mtime_epoch}|{entry.category}\n".encode("utf-8"))
    return h.hexdigest()[:16]
