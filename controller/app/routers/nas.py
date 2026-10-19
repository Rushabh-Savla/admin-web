from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import User
from controller.app.dependencies import require_role
from controller.app.nas_service import browse_nas_directories, test_nas_connection, get_nas_directory_summary

router = APIRouter(prefix="/api/v1/nas", tags=["NAS Explorer & Storage"])


@router.get("/browse")
def browse_nas(
    path: str = Query("", description="Relative path within the NAS to browse"),
    remote: Optional[str] = Query(None, description="Remote name (defaults to configured remote)"),
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
):
    """
    Browse directories and inspect contents on the NAS remote.
    Allows administrators and managers to discover directory trees and select directories.
    """
    result = browse_nas_directories(remote_name=remote, subpath=path)
    if result["status"] == "ERROR":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result["error"],
        )
    return result


@router.get("/directory-summary")
def directory_summary(
    path: str = Query("", description="Directory path on NAS to summarize"),
    remote: Optional[str] = Query(None, description="Remote name (defaults to configured remote)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
):
    """
    Get file counts and category breakdown for a selected NAS directory.
    Distinguishes direct vs recursive counts and image vs non-image file types.
    """
    result = get_nas_directory_summary(remote_name=remote, subpath=path, db=db)
    if result.get("status") == "ERROR":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=result.get("error", "Failed to retrieve directory summary"),
        )
    return result


@router.post("/test")
def test_connection(
    remote: Optional[str] = None,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
):
    """Test reachability to the configured NAS storage remote."""
    return test_nas_connection(remote_name=remote, force=True)


@router.get("/status")
def get_nas_status(
    remote: Optional[str] = None,
):
    """
    Check real-time connectivity to the configured NAS storage remote.
    Accessible to dashboard clients and annotator workstations for live health monitoring.
    """
    return test_nas_connection(remote_name=remote)



