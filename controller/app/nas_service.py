import os
import time
import subprocess
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from controller.app.config import settings

logger = logging.getLogger(__name__)


def obscure_password(password: str, rclone_bin: str = "rclone") -> str:
    """Obscure password using rclone obscure command, or return raw if failure."""
    if not password:
        return ""
    try:
        proc = subprocess.run(
            [rclone_bin, "obscure", password],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception as exc:
        logger.warning(f"Failed to obscure password via rclone: {exc}")
    return password


def sync_rclone_config_from_env():
    """
    If NAS credentials (NAS_HOST, NAS_USER, etc.) are provided in environment / .env,
    automatically generate and write the corresponding [annotation_nas] configuration
    into secrets/rclone.conf with mode 600.
    """
    if not settings.NAS_HOST and settings.NAS_STORAGE_TYPE != "local":
        return

    config_path = Path(settings.RCLONE_CONFIG_PATH)
    config_dir = config_path.parent
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    # Obscure password if provided
    obscured_pass = obscure_password(settings.NAS_PASSWORD, settings.RCLONE_BIN) if settings.NAS_PASSWORD else ""

    lines = []
    lines.append(f"[{settings.DEFAULT_REMOTE_NAME}]")
    lines.append(f"type = {settings.NAS_STORAGE_TYPE}")

    if settings.NAS_STORAGE_TYPE == "sftp":
        if settings.NAS_HOST:
            lines.append(f"host = {settings.NAS_HOST}")
        lines.append(f"port = {settings.NAS_PORT}")
        if settings.NAS_USER:
            lines.append(f"user = {settings.NAS_USER}")
        if obscured_pass:
            lines.append(f"pass = {obscured_pass}")
        if settings.NAS_KEY_FILE:
            lines.append(f"key_file = {settings.NAS_KEY_FILE}")
        lines.append("shell_type = unix")
        lines.append("md5sum_command = none")
        lines.append("sha1sum_command = none")

    elif settings.NAS_STORAGE_TYPE == "smb":
        if settings.NAS_HOST:
            lines.append(f"host = {settings.NAS_HOST}")
        lines.append(f"port = {settings.NAS_PORT}")
        if settings.NAS_USER:
            lines.append(f"user = {settings.NAS_USER}")
        if obscured_pass:
            lines.append(f"pass = {obscured_pass}")

    elif settings.NAS_STORAGE_TYPE == "webdav":
        url = f"http://{settings.NAS_HOST}:{settings.NAS_PORT}"
        lines.append(f"url = {url}")
        lines.append("vendor = other")
        if settings.NAS_USER:
            lines.append(f"user = {settings.NAS_USER}")
        if obscured_pass:
            lines.append(f"pass = {obscured_pass}")

    elif settings.NAS_STORAGE_TYPE == "local":
        lines.append("type = local")

    # Add duplicate section name for restore operations to avoid Rclone overlapping remotes checks
    remote_block = list(lines)
    lines.append("")
    lines.append(f"[{settings.DEFAULT_REMOTE_NAME}_restore]")
    for l in remote_block[1:]:
        lines.append(l)

    new_content = "\n".join(lines) + "\n"

    try:
        config_path.write_text(new_content)
        os.chmod(config_path, 0o600)
        logger.info(f"Successfully synchronized Rclone configuration from .env for remote [{settings.DEFAULT_REMOTE_NAME}]")
    except Exception as exc:
        logger.error(f"Failed to write Rclone config to {config_path}: {exc}")


def resolve_rclone_config_path() -> str:
    """Resolve active rclone.conf path, falling back to local secrets/ directory if needed."""
    cfg = settings.RCLONE_CONFIG_PATH
    if os.path.exists(cfg) and os.path.getsize(cfg) > 0:
        return cfg
    local_cfg = Path("secrets/rclone.conf").resolve()
    if local_cfg.exists() and local_cfg.stat().st_size > 0:
        return str(local_cfg)
    return cfg


_nas_conn_cache: Dict[str, Any] = {"timestamp": 0.0, "result": None}


def test_nas_connection(remote_name: Optional[str] = None, force: bool = False, cache_ttl: float = 300.0) -> Dict[str, Any]:
    """Test connectivity to the configured NAS remote with 300s in-memory TTL caching to protect NAS from excessive SSH/SFTP traffic."""
    global _nas_conn_cache
    now = time.time()
    if not force and not remote_name and _nas_conn_cache["result"] is not None and (now - _nas_conn_cache["timestamp"] < cache_ttl):
        return _nas_conn_cache["result"]

    sync_rclone_config_from_env()
    remote = remote_name or settings.DEFAULT_REMOTE_NAME
    config_path = resolve_rclone_config_path()

    # Determine probe target
    test_path = settings.NAS_ROOT_PATH.strip() if settings.NAS_ROOT_PATH else ""
    if test_path:
        target = f"{remote}:{test_path}"
    else:
        target = f"{remote}:"

    cmd = [
        settings.RCLONE_BIN,
        "about",
        target,
        "--config",
        config_path,
        "--timeout",
        "5s",
        "--contimeout",
        "4s",
    ]
    res: Dict[str, Any] = {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if proc.returncode == 0:
            res = {"connected": True, "remote": remote, "target": target, "message": "NAS storage is reachable"}
        else:
            # Fallback to lsf if 'about' is not supported by backend
            proc_lsf = subprocess.run(
                [
                    settings.RCLONE_BIN,
                    "lsf",
                    target,
                    "--max-depth",
                    "1",
                    "--config",
                    config_path,
                    "--timeout",
                    "5s",
                    "--contimeout",
                    "4s",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc_lsf.returncode == 0:
                res = {"connected": True, "remote": remote, "target": target, "message": "NAS storage is reachable"}
            else:
                err_msg = proc_lsf.stderr.strip() or proc.stderr.strip() or "Connection failed"
                res = {
                    "connected": False,
                    "remote": remote,
                    "target": target,
                    "error": err_msg,
                }
    except Exception as exc:
        res = {"connected": False, "remote": remote, "target": target, "error": str(exc)}

    if not remote_name:
        _nas_conn_cache["timestamp"] = time.time()
        _nas_conn_cache["result"] = res
    return res


def browse_nas_directories(
    remote_name: Optional[str] = None,
    subpath: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Browse directories and sample files on the NAS for the admin UI directory picker.
    Properly handles absolute SFTP paths (e.g. /dataset) and relative paths.
    """
    sync_rclone_config_from_env()
    remote = remote_name or settings.DEFAULT_REMOTE_NAME
    config_path = resolve_rclone_config_path()

    # Normalize requested path
    if subpath is None or subpath == "":
        current_path = settings.NAS_ROOT_PATH.strip() if settings.NAS_ROOT_PATH else ""
    else:
        current_path = subpath.strip()

    # Build rclone target
    # In SFTP: remote:/abs/path is absolute, remote:rel/path is relative to user home
    if current_path:
        target = f"{remote}:{current_path}"
    else:
        target = f"{remote}:"

    cmd = [
        settings.RCLONE_BIN,
        "lsjson",
        target,
        "--max-depth",
        "1",
        "--no-mimetype",
        "--timeout",
        "5s",
        "--contimeout",
        "4s",
    ]
    if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
        cmd.extend(["--config", config_path])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            err = proc.stderr.strip()
            logger.warning(f"Rclone browse failed for target '{target}': {err}")
            return {
                "status": "ERROR",
                "error": err or f"Failed to list directory on NAS (code {proc.returncode})",
                "current_path": current_path,
                "directories": [],
                "images": [],
            }

        items = json.loads(proc.stdout or "[]")
        directories = []
        images = []
        image_extensions = settings.SUPPORTED_IMAGE_EXTENSIONS

        is_absolute = current_path.startswith("/")

        for it in items:
            name = it.get("Name", "")
            is_dir = it.get("IsDir", False)
            
            # Reconstruct item path preserving absolute/relative format
            if current_path in ("", "/"):
                item_rel = f"/{name}" if is_absolute else name
            else:
                item_rel = f"{current_path.rstrip('/')}/{name}"

            if is_dir:
                directories.append({
                    "name": name,
                    "relative_path": item_rel,
                    "mod_time": it.get("ModTime"),
                })
            else:
                ext = Path(name).suffix.lower()
                clean_ext = ext.lstrip(".")
                if ext in image_extensions or clean_ext in image_extensions or f".{clean_ext}" in image_extensions:
                    images.append({
                        "name": name,
                        "relative_path": item_rel,
                        "size": it.get("Size", 0),
                        "mod_time": it.get("ModTime"),
                    })

        # Calculate parent path for 'Go Up' navigation
        parent_path = None
        norm_path = current_path.rstrip("/")
        if norm_path and norm_path != "/":
            parent = str(Path(norm_path).parent)
            # If path was absolute and parent became '.', fix to '/'
            if is_absolute and parent == ".":
                parent_path = "/"
            elif not is_absolute and parent == ".":
                parent_path = ""
            else:
                parent_path = parent
        elif norm_path == "/" or current_path == "":
            parent_path = None

        return {
            "status": "OK",
            "current_path": current_path,
            "parent_path": parent_path,
            "directories": sorted(directories, key=lambda x: x["name"]),
            "images_count": len(images),
            "sample_images": images[:10],
        }

    except Exception as exc:
        logger.error(f"Exception browsing NAS target '{target}': {exc}")
        return {
            "status": "ERROR",
            "error": str(exc),
            "current_path": current_path,
            "directories": [],
            "images": [],
        }


def get_nas_directory_summary(
    remote_name: Optional[str] = None,
    subpath: Optional[str] = None,
    db: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Get detailed summary counts for a selected NAS directory:
    total images, available, assigned, completed, and breakdown by file type (direct vs recursive).
    """
    sync_rclone_config_from_env()
    remote = remote_name or settings.DEFAULT_REMOTE_NAME
    current_path = (subpath or "").strip()
    norm_path = current_path.strip("/")
    config_path = resolve_rclone_config_path()

    # 1. If DB session provided, check if path matches a registered Dataset
    if db:
        from controller.app.models import Dataset, DatasetFile, DatasetDirectory, ActiveImageClaim, AssignmentItem
        datasets = db.query(Dataset).all()
        for ds in datasets:
            ds_norm = ds.remote_path.strip("/")
            if norm_path == ds_norm or norm_path.startswith(ds_norm + "/"):
                scope_rel = norm_path[len(ds_norm):].strip("/")
                dir_rec = (
                    db.query(DatasetDirectory)
                    .filter(DatasetDirectory.dataset_id == ds.id, DatasetDirectory.normalized_path == scope_rel)
                    .first()
                )

                files_query = db.query(DatasetFile).filter(
                    DatasetFile.dataset_id == ds.id,
                    DatasetFile.category == "IMAGE",
                )
                if scope_rel:
                    files_query = files_query.filter(
                        (DatasetFile.normalized_path == scope_rel) |
                        (DatasetFile.normalized_path.startswith(scope_rel + "/"))
                    )
                all_images = files_query.all()
                total_imgs = len(all_images)

                file_ids = [f.id for f in all_images]
                active_claims = (
                    db.query(ActiveImageClaim)
                    .filter(ActiveImageClaim.file_id.in_(file_ids))
                    .count() if file_ids else 0
                )
                completed_count = (
                    db.query(AssignmentItem)
                    .filter(
                        AssignmentItem.file_id.in_(file_ids),
                        AssignmentItem.item_state == "VERIFIED_COMPLETE"
                    )
                    .count() if file_ids else 0
                )
                available_count = max(0, total_imgs - active_claims - completed_count)

                type_query = db.query(DatasetFile).filter(DatasetFile.dataset_id == ds.id)
                if scope_rel:
                    type_query = type_query.filter(
                        (DatasetFile.normalized_path == scope_rel) |
                        (DatasetFile.normalized_path.startswith(scope_rel + "/"))
                    )
                all_files = type_query.all()

                jpg_count = sum(1 for f in all_files if f.extension.lower() in ("jpg", "jpeg"))
                png_count = sum(1 for f in all_files if f.extension.lower() == "png")
                json_count = sum(1 for f in all_files if f.category == "JSON")
                txt_count = sum(1 for f in all_files if f.category == "TXT")
                other_count = sum(1 for f in all_files if f.category not in ("IMAGE", "JSON", "TXT"))

                direct_imgs = dir_rec.direct_image_count if dir_rec else (total_imgs if not scope_rel else 0)
                direct_json = dir_rec.direct_json_count if dir_rec else 0
                direct_txt = dir_rec.direct_txt_count if dir_rec else 0
                direct_other = dir_rec.direct_other_count if dir_rec else 0

                rec_imgs = dir_rec.recursive_image_count if dir_rec else total_imgs
                rec_json = dir_rec.recursive_json_count if dir_rec else json_count
                rec_txt = dir_rec.recursive_txt_count if dir_rec else txt_count
                rec_other = dir_rec.recursive_other_count if dir_rec else other_count

                return {
                    "status": "OK",
                    "directory": Path(current_path).name or norm_path or "root",
                    "full_path": current_path,
                    "dataset_id": ds.id,
                    "dataset_name": ds.name,
                    "is_registered": True,
                    "total_images": total_imgs,
                    "available_images": available_count,
                    "assigned_images": active_claims,
                    "completed_images": completed_count,
                    "direct": {
                        "images": direct_imgs,
                        "json": direct_json,
                        "txt": direct_txt,
                        "other": direct_other,
                    },
                    "recursive": {
                        "images": rec_imgs,
                        "json": rec_json,
                        "txt": rec_txt,
                        "other": rec_other,
                    },
                    "file_types": {
                        "jpg_jpeg": jpg_count,
                        "png": png_count,
                        "json": json_count,
                        "txt": txt_count,
                        "other": other_count,
                    },
                }

    # 2. If not registered in DB, query live via Rclone
    target = f"{remote}:{current_path}" if current_path else f"{remote}:"
    cmd = [
        settings.RCLONE_BIN,
        "lsjson",
        target,
        "--recursive",
        "--files-only",
        "--no-mimetype",
        "--timeout", "15s",
        "--contimeout", "5s",
    ]
    if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
        cmd.extend(["--config", config_path])

    cmd.extend([
        "--exclude", "**/assignment_*/**",
        "--exclude", ".Trash*/**",
        "--exclude", "**/.Trash*/**",
        "--exclude", "#Recycle/**",
        "--exclude", "**/#Recycle/**",
        "--exclude", "@eaDir/**",
        "--exclude", "**/@eaDir/**",
        "--ignore-errors",
    ])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0 and not proc.stdout.strip():
            return {
                "status": "ERROR",
                "error": proc.stderr.strip() or f"Failed to scan directory (code {proc.returncode})",
                "directory": Path(current_path).name or norm_path or "root",
                "full_path": current_path,
            }

        items = json.loads(proc.stdout or "[]")
        image_extensions = settings.SUPPORTED_IMAGE_EXTENSIONS

        direct_imgs = 0
        direct_json = 0
        direct_txt = 0
        direct_other = 0

        rec_imgs = 0
        rec_json = 0
        rec_txt = 0
        rec_other = 0

        jpg_count = 0
        png_count = 0

        for it in items:
            raw_path = it.get("Path") or it.get("Name", "")
            is_direct = "/" not in raw_path
            name = it.get("Name") or Path(raw_path).name
            ext = Path(name).suffix.lower()
            clean_ext = ext.lstrip(".")

            is_img = ext in image_extensions or clean_ext in image_extensions or f".{clean_ext}" in image_extensions
            is_json = ext == ".json"
            is_txt = ext == ".txt"

            if is_img:
                rec_imgs += 1
                if ext in (".jpg", ".jpeg"):
                    jpg_count += 1
                elif ext == ".png":
                    png_count += 1
                if is_direct:
                    direct_imgs += 1
            elif is_json:
                rec_json += 1
                if is_direct:
                    direct_json += 1
            elif is_txt:
                rec_txt += 1
                if is_direct:
                    direct_txt += 1
            else:
                rec_other += 1
                if is_direct:
                    direct_other += 1

        return {
            "status": "OK",
            "directory": Path(current_path).name or norm_path or "root",
            "full_path": current_path,
            "is_registered": False,
            "total_images": rec_imgs,
            "available_images": rec_imgs,
            "assigned_images": 0,
            "completed_images": 0,
            "direct": {
                "images": direct_imgs,
                "json": direct_json,
                "txt": direct_txt,
                "other": direct_other,
            },
            "recursive": {
                "images": rec_imgs,
                "json": rec_json,
                "txt": rec_txt,
                "other": rec_other,
            },
            "file_types": {
                "jpg_jpeg": jpg_count,
                "png": png_count,
                "json": rec_json,
                "txt": rec_txt,
                "other": rec_other,
            },
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "error": str(exc),
            "directory": Path(current_path).name or norm_path or "root",
            "full_path": current_path,
        }

