from datetime import datetime, timezone
import logging
import re
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse
from fastapi import APIRouter, Depends, Request, Response, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from controller.app.database import get_db
from controller.app.models import (
    User,
    Device,
    Dataset,
    DatasetDirectory,
    DatasetFile,
    Assignment,
    AssignmentItem,
    ActiveImageClaim,
    AnnotationSubmission,
    BackgroundDeclaration,
    SyncJob,
    AuditEvent,
)
from controller.app.auth import verify_password, hash_password, create_access_token
from controller.app.dependencies import get_current_user_optional, require_role
from controller.app.assignment_service import (
    create_atomic_assignment,
    pause_assignment,
    resume_assignment,
    cancel_assignment,
    restore_and_cleanup_dataset,
)
from controller.app.config import settings

templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

router = APIRouter(include_in_schema=False)

logger = logging.getLogger(__name__)


@router.get("/", response_class=HTMLResponse)
def root_redirect():
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"current_user": None, "error": None},
    )


@router.post("/dashboard/login", response_class=HTMLResponse)
def login_action(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash) or not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"current_user": None, "error": "Invalid username or password, or account disabled."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_access_token(data={"sub": user.username, "role": user.role, "uid": user.id})
    redirect = RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)
    is_https = request.url.scheme == "https"
    redirect.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_https,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return redirect


@router.get("/dashboard/logout")
def logout_action():
    resp = RedirectResponse(url="/dashboard/login", status_code=status.HTTP_302_FOUND)
    resp.delete_cookie(key=settings.SESSION_COOKIE_NAME)
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
def overview_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    # 5 Main Overview Statistics requested by specification
    total_projects = db.query(Dataset).count()
    total_images = db.query(DatasetFile).filter(DatasetFile.category == "IMAGE").count()
    assigned_images = db.query(ActiveImageClaim).count()
    completed_images = db.query(AssignmentItem).filter(AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING", "BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"])).count()
    available_images = max(0, total_images - assigned_images - completed_images)

    stats = {
        "total_projects": total_projects,
        "dataset_count": total_projects,
        "total_images": total_images,
        "available_images": available_images,
        "assigned_images": assigned_images,
        "completed_images": completed_images,
        "active_assignments": db.query(Assignment).filter(Assignment.status.in_(["READY", "IN_PROGRESS", "PREPARING"])).count(),
        "verified_complete": completed_images,
        "in_progress": db.query(AssignmentItem).filter(AssignmentItem.item_state.in_(["PENDING", "DOWNLOADING", "ANNOTATING"])).count(),
        "review_count": db.query(AssignmentItem).filter(AssignmentItem.item_state.in_(["FAILED", "CONFLICT"])).count(),
        "active_annotators": db.query(User).filter(User.role == "ANNOTATOR", User.is_active.is_(True)).count(),
    }

    # Project summaries for simple cards/rows on main dashboard
    projects = []
    datasets = db.query(Dataset).order_by(Dataset.id.desc()).all()
    for ds in datasets:
        ds_total = db.query(DatasetFile).filter(DatasetFile.dataset_id == ds.id, DatasetFile.category == "IMAGE").count()
        ds_assigned = (
            db.query(func.count(ActiveImageClaim.file_id))
            .join(DatasetFile, ActiveImageClaim.file_id == DatasetFile.id)
            .filter(DatasetFile.dataset_id == ds.id)
            .scalar() or 0
        )
        ds_completed = (
            db.query(func.count(AssignmentItem.id))
            .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
            .filter(DatasetFile.dataset_id == ds.id, AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING", "BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]))
            .scalar() or 0
        )
        ds_available = max(0, ds_total - ds_assigned - ds_completed)
        progress = int((ds_completed / ds_total) * 100) if ds_total > 0 else 0

        # Collect assignments belonging to this project folder
        project_assignments = []
        for a in ds.assignments:
            comp_cnt = (
                db.query(func.count(AssignmentItem.id))
                .filter(
                    AssignmentItem.assignment_id == a.id,
                    AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING", "BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]),
                )
                .scalar() or 0
            )
            a_progress = int((comp_cnt / a.allocated_count) * 100) if a.allocated_count > 0 else 0
            project_assignments.append({
                "id": a.id,
                "directory_scope": a.directory_scope,
                "scope_type": a.scope_type,
                "annotator": a.annotator,
                "allocated_count": a.allocated_count,
                "completed_count": comp_cnt,
                "progress_percent": a_progress,
                "status": a.status,
                "created_at": a.created_at,
            })

        pending_count = sum(1 for a in ds.assignments if a.status == "SUBMITTED_FOR_REVIEW")
        is_fully_completed = bool(progress == 100 and ds_total > 0 and ds_available == 0)
        proj_status = "COMPLETED" if is_fully_completed else (ds.project_status or "ACTIVE")
        if is_fully_completed and ds.project_status != "COMPLETED":
            try:
                ds.project_status = "COMPLETED"
                db.commit()
            except Exception:
                pass

        projects.append({
            "id": ds.id,
            "title": ds.title or ds.name.replace("_", " ").title(),
            "name": ds.name,
            "description": ds.description,
            "assigned_under": ds.assigned_under or "Unassigned",
            "project_status": proj_status,
            "remote_name": ds.remote_name,
            "remote_path": ds.remote_path,
            "total_images": ds_total,
            "available_images": ds_available,
            "assigned_images": ds_assigned,
            "completed_images": ds_completed,
            "progress_percent": progress,
            "status": ds.catalog_state,
            "created_at": ds.created_at,
            "assignments": project_assignments,
            "assignment_count": len(project_assignments),
            "pending_completion_count": pending_count,
        })

    recent_assignments = (
        db.query(Assignment)
        .order_by(Assignment.id.desc())
        .limit(10)
        .all()
    )

    # Master Completion Approval Triggers
    pending_completions = (
        db.query(Assignment)
        .filter(Assignment.status == "SUBMITTED_FOR_REVIEW")
        .order_by(Assignment.updated_at.desc())
        .all()
    )
    for a in pending_completions:
        a.background_count = (
            db.query(func.count(AssignmentItem.id))
            .filter(
                AssignmentItem.assignment_id == a.id,
                AssignmentItem.item_state.in_(["BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]),
            )
            .scalar() or 0
        )
        a.object_count = (
            db.query(func.count(AssignmentItem.id))
            .filter(
                AssignmentItem.assignment_id == a.id,
                AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING"]),
            )
            .scalar() or 0
        )
        a.completed_count = a.background_count + a.object_count

    # Review Queue Items for embedding inside Projects section: Genuine file failures and conflicts
    review_items_query = (
        db.query(AssignmentItem, DatasetFile, Assignment)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .join(Assignment, AssignmentItem.assignment_id == Assignment.id)
        .filter(AssignmentItem.item_state.in_(["FAILED", "CONFLICT"]))
        .all()
    )
    reviews_list = [
        {
            "item_id": itm.id,
            "assignment_id": assign.id,
            "file_path": f.normalized_path,
            "state": itm.item_state,
            "error": itm.error,
        }
        for itm, f, assign in review_items_query
    ]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": current_user,
            "stats": stats,
            "projects": projects,
            "recent_assignments": recent_assignments,
            "pending_completions": pending_completions,
            "reviews": reviews_list,
            "pending_triggers_count": len(pending_completions) + len(reviews_list),
            "active_tab": request.query_params.get("tab", "projects"),
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/dashboard/projects", response_class=HTMLResponse)
def projects_list_redirect():
    return RedirectResponse(url="/dashboard", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/projects/new", response_class=HTMLResponse)
def project_create_page(
    request: Request,
    path: Optional[str] = None,
    name: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")
    if current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    initial_root = settings.NAS_ROOT_PATH.strip() if settings.NAS_ROOT_PATH else ""
    users = db.query(User).filter(User.is_active == True).order_by(User.username.asc()).all()

    prefilled_path = (path or "").strip()
    prefilled_title = ""
    prefilled_slug = ""
    if prefilled_path:
        raw_name = (name or "").strip() or prefilled_path.split("/")[-1] or "project"
        prefilled_title = raw_name.replace("_", " ").replace("-", " ").title()
        prefilled_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', raw_name.lower()).strip('_')

    return templates.TemplateResponse(
        request=request,
        name="project_create.html",
        context={
            "current_user": current_user,
            "default_remote": settings.DEFAULT_REMOTE_NAME,
            "default_root_path": initial_root,
            "users": users,
            "prefilled_path": prefilled_path,
            "prefilled_title": prefilled_title,
            "prefilled_slug": prefilled_slug,
            "error": request.query_params.get("error"),
        },
    )


@router.get("/dashboard/datasets", response_class=HTMLResponse)
def datasets_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    datasets = db.query(Dataset).order_by(Dataset.id.asc()).all()
    initial_root = settings.NAS_ROOT_PATH.strip() if settings.NAS_ROOT_PATH else ""

    return templates.TemplateResponse(
        request=request,
        name="datasets.html",
        context={
            "current_user": current_user,
            "datasets": datasets,
            "default_remote": settings.DEFAULT_REMOTE_NAME,
            "default_root_path": initial_root,
        },
    )


@router.post("/dashboard/projects/new")
@router.post("/dashboard/datasets/new")
def create_dataset_form(
    title: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    assigned_under: Optional[str] = Form(None),
    project_status: str = Form("ACTIVE"),
    remote_name: str = Form("annotation_nas"),
    remote_path: str = Form(...),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    clean_path = remote_path.strip()
    if clean_path.startswith(("sftp://", "smb://", "webdav://", "http://", "https://")):
        clean_path = urlparse(clean_path).path.lstrip("/")

    clean_title = (title or "").strip()
    clean_name = (name or "").strip()

    if not clean_name:
        if clean_title:
            import re
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', clean_title.lower()).strip('_')
        else:
            clean_name = clean_path.split("/")[-1] or "project"

    if not clean_title:
        clean_title = clean_name.replace("_", " ").title()

    clean_assigned_under = (assigned_under or "").strip() or (current_user.username if current_user else "admin")

    # Check for existing dataset with same name
    existing = db.query(Dataset).filter(Dataset.name == clean_name).first()
    if existing:
        # If it points to the same remote and path, trigger a fresh scan and redirect with notice
        if existing.remote_name == remote_name.strip() and existing.remote_path == clean_path:
            existing.catalog_state = "SCANNING"
            existing.title = clean_title
            if description and description.strip():
                existing.description = description.strip()
            existing.assigned_under = clean_assigned_under
            existing.project_status = project_status.strip() if project_status else "ACTIVE"
            job = SyncJob(
                job_type="CATALOG_SCAN",
                status="QUEUED",
                payload={"dataset_id": existing.id},
            )
            db.add(job)
            db.commit()
            return RedirectResponse(
                url=f"/dashboard?notice=Project+'{quote_plus(clean_title)}'+updated.+Catalog+scan+enqueued.",
                status_code=status.HTTP_302_FOUND,
            )
        else:
            # Different target path with same name: auto-disambiguate name
            base_name = clean_name
            idx = 2
            while db.query(Dataset).filter(Dataset.name == clean_name).first():
                clean_name = f"{base_name}_{idx}"
                idx += 1

    try:
        ds = Dataset(
            title=clean_title,
            name=clean_name,
            description=(description or "").strip() or None,
            assigned_under=clean_assigned_under,
            project_status=project_status.strip() if project_status else "ACTIVE",
            remote_name=remote_name.strip() if remote_name else settings.DEFAULT_REMOTE_NAME,
            remote_path=clean_path,
            catalog_state="SCANNING",
        )
        db.add(ds)
        db.flush()

        job = SyncJob(
            job_type="CATALOG_SCAN",
            status="QUEUED",
            payload={"dataset_id": ds.id},
        )
        db.add(job)
        db.commit()
        return RedirectResponse(
            url=f"/dashboard?notice=Project+'{quote_plus(clean_title)}'+created+successfully.+Catalog+scan+in+progress.",
            status_code=status.HTTP_302_FOUND,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/dashboard/projects/new?error={quote_plus('Failed to create project: ' + str(exc))}",
            status_code=status.HTTP_302_FOUND,
        )


@router.post("/dashboard/projects/{dataset_id}/delete")
@router.post("/dashboard/datasets/{dataset_id}/delete")
def delete_project_action(
    dataset_id: int,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    proj = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not proj:
        return RedirectResponse(
            url="/dashboard?error=Project+not+found",
            status_code=status.HTTP_302_FOUND,
        )

    title = proj.title or proj.name
    try:
        # 1. Restore all staged images from annotator workspaces back to the original dataset directory structure on NAS
        try:
            restore_and_cleanup_dataset(db, dataset_id)
        except Exception as r_err:
            logger.warning(f"Note during dataset {dataset_id} restoration (proceeding with deletion): {r_err}")

        # 2. Cascade delete all related assignments, submissions, claims, items, files, directories, and sync jobs
        assignments = db.query(Assignment).filter(Assignment.dataset_id == dataset_id).all()
        assignment_ids = [a.id for a in assignments]
        files = db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset_id).all()
        file_ids = [f.id for f in files]

        if assignment_ids or file_ids:
            conds_sub = []
            conds_bg = []
            conds_claim = []
            if assignment_ids:
                conds_sub.append(AnnotationSubmission.assignment_id.in_(assignment_ids))
                conds_bg.append(BackgroundDeclaration.assignment_id.in_(assignment_ids))
                conds_claim.append(ActiveImageClaim.assignment_id.in_(assignment_ids))
            if file_ids:
                conds_sub.append(AnnotationSubmission.file_id.in_(file_ids))
                conds_bg.append(BackgroundDeclaration.file_id.in_(file_ids))
                conds_claim.append(ActiveImageClaim.file_id.in_(file_ids))

            if conds_sub:
                db.query(AnnotationSubmission).filter(or_(*conds_sub)).delete(synchronize_session=False)
            if conds_bg:
                db.query(BackgroundDeclaration).filter(or_(*conds_bg)).delete(synchronize_session=False)
            if conds_claim:
                db.query(ActiveImageClaim).filter(or_(*conds_claim)).delete(synchronize_session=False)

        if assignment_ids:
            db.query(AssignmentItem).filter(AssignmentItem.assignment_id.in_(assignment_ids)).delete(synchronize_session=False)
            db.query(Assignment).filter(Assignment.id.in_(assignment_ids)).delete(synchronize_session=False)

        db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset_id).delete(synchronize_session=False)
        db.query(DatasetDirectory).filter(DatasetDirectory.dataset_id == dataset_id).delete(synchronize_session=False)

        all_jobs = db.query(SyncJob).all()
        for j in all_jobs:
            if isinstance(j.payload, dict) and j.payload.get("dataset_id") == dataset_id:
                db.delete(j)

        try:
            db.delete(proj)
        except Exception:
            db.query(Dataset).filter(Dataset.id == dataset_id).delete(synchronize_session=False)

        try:
            audit = AuditEvent(
                user_id=current_user.id,
                action="PROJECT_DELETED",
                resource_type="dataset",
                resource_id=str(dataset_id),
                details={"title": title, "name": getattr(proj, "name", "dataset")},
            )
            db.add(audit)
        except Exception:
            pass

        db.commit()

        return RedirectResponse(
            url=f"/dashboard?notice=Project+'{quote_plus(title)}'+and+all+its+assignments+were+deleted+successfully.",
            status_code=status.HTTP_302_FOUND,
        )
    except Exception as exc:
        db.rollback()
        return RedirectResponse(
            url=f"/dashboard?error=Failed+to+delete+project:+{quote_plus(str(exc))}",
            status_code=status.HTTP_302_FOUND,
        )


@router.post("/dashboard/projects/{dataset_id}/consolidate")
@router.post("/dashboard/datasets/{dataset_id}/consolidate")
def dashboard_consolidate_project(
    dataset_id: int,
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        return RedirectResponse(url="/dashboard/login")

    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        return RedirectResponse(url="/dashboard?error=Project+not+found", status_code=status.HTTP_302_FOUND)

    try:
        from controller.app.assignment_service import consolidate_dataset_completed_assignments
        res = consolidate_dataset_completed_assignments(db, dataset_id)
        cnt = res.get("consolidated", 0)
        return RedirectResponse(
            url=f"/dashboard?notice=Successfully+consolidated+{cnt}+assignment+workspaces+into+the+main+folder.",
            status_code=status.HTTP_302_FOUND,
        )
    except Exception as exc:
        return RedirectResponse(
            url=f"/dashboard?error=Error+consolidating+workspaces:+{quote_plus(str(exc))}",
            status_code=status.HTTP_302_FOUND,
        )


@router.get("/dashboard/projects/{dataset_id}", response_class=HTMLResponse)
@router.get("/dashboard/datasets/{dataset_id}", response_class=HTMLResponse)
def dataset_detail_page(
    dataset_id: int,
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(status_code=404)

    directories = (
        db.query(DatasetDirectory)
        .filter(DatasetDirectory.dataset_id == dataset_id)
        .order_by(DatasetDirectory.normalized_path.asc())
        .all()
    )

    total_imgs = db.query(DatasetFile).filter(DatasetFile.dataset_id == dataset_id, DatasetFile.category == "IMAGE").count()
    assigned_imgs = (
        db.query(func.count(ActiveImageClaim.file_id))
        .join(DatasetFile, ActiveImageClaim.file_id == DatasetFile.id)
        .filter(DatasetFile.dataset_id == dataset_id)
        .scalar() or 0
    )
    completed_imgs = (
        db.query(func.count(AssignmentItem.id))
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .filter(DatasetFile.dataset_id == dataset_id, AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING", "BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]))
        .scalar() or 0
    )
    available_imgs = max(0, total_imgs - assigned_imgs - completed_imgs)

    annotators = db.query(User).filter(User.role == "ANNOTATOR", User.is_active.is_(True)).order_by(User.username.asc()).all()
    assignments = db.query(Assignment).filter(Assignment.dataset_id == dataset_id).order_by(Assignment.id.desc()).all()
    progress_percent = int((completed_imgs / total_imgs) * 100) if total_imgs > 0 else 0

    return templates.TemplateResponse(
        request=request,
        name="dataset_detail.html",
        context={
            "current_user": current_user,
            "dataset": ds,
            "directories": directories,
            "total_images": total_imgs,
            "available_images": available_imgs,
            "assigned_images": assigned_imgs,
            "completed_images": completed_imgs,
            "progress_percent": progress_percent,
            "annotators": annotators,
            "assignments": assignments,
            "notice": request.query_params.get("notice"),
            "error": request.query_params.get("error"),
            "created": request.query_params.get("created") == "true",
        },
    )



@router.get("/dashboard/assignments", response_class=HTMLResponse)
def assignments_page(
    request: Request,
    dataset_id: Optional[int] = None,
    project_id: Optional[int] = None,
    scope: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    target_project_id = project_id or dataset_id
    assignments = db.query(Assignment).order_by(Assignment.id.desc()).all()
    datasets = db.query(Dataset).order_by(Dataset.id.desc()).all()
    for ds in datasets:
        ds_tot = db.query(DatasetFile).filter(DatasetFile.dataset_id == ds.id, DatasetFile.category == "IMAGE").count()
        ds_asg = (
            db.query(func.count(ActiveImageClaim.file_id))
            .join(DatasetFile, ActiveImageClaim.file_id == DatasetFile.id)
            .filter(DatasetFile.dataset_id == ds.id)
            .scalar() or 0
        )
        ds_cmp = (
            db.query(func.count(AssignmentItem.id))
            .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
            .filter(DatasetFile.dataset_id == ds.id, AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING", "BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]))
            .scalar() or 0
        )
        ds.available_images = max(0, ds_tot - ds_asg - ds_cmp)
        ds.total_images = ds_tot

    annotators = db.query(User).filter(User.role == "ANNOTATOR", User.is_active.is_(True)).all()

    return templates.TemplateResponse(
        request=request,
        name="assignments.html",
        context={
            "current_user": current_user,
            "assignments": assignments,
            "datasets": datasets,
            "annotators": annotators,
            "selected_dataset_id": target_project_id,
            "selected_scope": scope,
            "error": None,
        },
    )


@router.get("/dashboard/assignments/new", response_class=HTMLResponse)
def new_assignment_page(
    request: Request,
    dataset_id: Optional[int] = None,
    project_id: Optional[int] = None,
    scope: Optional[str] = None,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    return assignments_page(
        request=request,
        dataset_id=dataset_id,
        project_id=project_id,
        scope=scope,
        current_user=current_user,
        db=db,
    )


@router.post("/dashboard/assignments", response_class=HTMLResponse)
@router.post("/dashboard/assignments/new", response_class=HTMLResponse)
def create_assignment_form(
    request: Request,
    dataset_id: int = Form(...),
    directory_scope: str = Form(""),
    scope_type: str = Form("RECURSIVE"),
    annotator_id: int = Form(...),
    requested_count: int = Form(...),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    try:
        assignment = create_atomic_assignment(
            db=db,
            dataset_id=dataset_id,
            directory_scope=directory_scope,
            scope_type=scope_type,
            annotator_id=annotator_id,
            creator_id=current_user.id,
            requested_count=requested_count,
        )
        return RedirectResponse(url=f"/dashboard/assignments/{assignment.id}", status_code=status.HTTP_302_FOUND)
    except Exception as exc:
        assignments = db.query(Assignment).order_by(Assignment.id.desc()).all()
        datasets = db.query(Dataset).all()
        annotators = db.query(User).filter(User.role == "ANNOTATOR", User.is_active.is_(True)).all()
        return templates.TemplateResponse(
            request=request,
            name="assignments.html",
            context={
                "current_user": current_user,
                "assignments": assignments,
                "datasets": datasets,
                "annotators": annotators,
                "selected_dataset_id": dataset_id,
                "selected_scope": directory_scope,
                "error": str(exc),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@router.get("/dashboard/assignments/{assignment_id}", response_class=HTMLResponse)
def assignment_detail_page(
    assignment_id: int,
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        raise HTTPException(status_code=404)

    items = (
        db.query(AssignmentItem)
        .filter(AssignmentItem.assignment_id == assignment_id)
        .order_by(AssignmentItem.ordinal.asc())
        .all()
    )

    counts = {}
    for state, cnt in (
        db.query(AssignmentItem.item_state, func.count(AssignmentItem.id))
        .filter(AssignmentItem.assignment_id == assignment_id)
        .group_by(AssignmentItem.item_state)
        .all()
    ):
        counts[state] = cnt

    return templates.TemplateResponse(
        request=request,
        name="assignment_detail.html",
        context={
            "current_user": current_user,
            "assignment": assignment,
            "items": items,
            "status_counts": counts,
        },
    )


@router.post("/dashboard/assignments/{assignment_id}/pause")
def pause_assignment_action(
    assignment_id: int,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    pause_assignment(db, assignment_id, current_user.id)
    return RedirectResponse(url=f"/dashboard/assignments/{assignment_id}", status_code=status.HTTP_302_FOUND)


@router.post("/dashboard/assignments/{assignment_id}/resume")
def resume_assignment_action(
    assignment_id: int,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    resume_assignment(db, assignment_id, current_user.id)
    return RedirectResponse(url=f"/dashboard/assignments/{assignment_id}", status_code=status.HTTP_302_FOUND)


@router.post("/dashboard/assignments/{assignment_id}/cancel")
def cancel_assignment_action(
    assignment_id: int,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    cancel_assignment(db, assignment_id, current_user.id)
    return RedirectResponse(url=f"/dashboard/assignments/{assignment_id}", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        return RedirectResponse(url="/dashboard/login")

    users = db.query(User).order_by(User.id.asc()).all()
    devices = db.query(Device).all()
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={"current_user": current_user, "users": users, "devices": devices},
    )


@router.post("/dashboard/users/new")
def create_user_form(
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("ANNOTATOR"),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    normalized_username = username.strip()
    if not normalized_username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username is required")

    existing = db.query(User).filter(User.username == normalized_username).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Username '{normalized_username}' already exists")

    u = User(username=normalized_username, password_hash=hash_password(password), role=role)
    db.add(u)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Username '{normalized_username}' already exists")
    return RedirectResponse(url="/dashboard/users", status_code=status.HTTP_302_FOUND)


@router.post("/dashboard/users/{user_id}/toggle")
def toggle_user_status(
    user_id: int,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    u = db.query(User).filter(User.id == user_id).first()
    if u:
        u.is_active = not u.is_active
        db.commit()
    return RedirectResponse(url="/dashboard/users", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/reviews", response_class=HTMLResponse)
def reviews_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse(url="/dashboard/login")

    items = (
        db.query(AssignmentItem, DatasetFile, Assignment)
        .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
        .join(Assignment, AssignmentItem.assignment_id == Assignment.id)
        .filter(AssignmentItem.item_state.in_(["FAILED", "CONFLICT", "BACKGROUND_REVIEW"]))
        .all()
    )

    review_list = [
        {
            "item_id": itm.id,
            "assignment_id": assign.id,
            "file_path": f.normalized_path,
            "state": itm.item_state,
            "error": itm.error,
        }
        for itm, f, assign in items
    ]

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"current_user": current_user, "reviews": review_list},
    )


@router.get("/dashboard/operations", response_class=HTMLResponse)
def operations_page(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        return RedirectResponse(url="/dashboard/login")

    tab = request.query_params.get("tab", "jobs")
    jobs = db.query(SyncJob).order_by(SyncJob.id.desc()).limit(100).all()
    events = db.query(AuditEvent).order_by(AuditEvent.id.desc()).limit(100).all()
    return templates.TemplateResponse(
        request=request,
        name="operations.html",
        context={
            "current_user": current_user,
            "jobs": jobs,
            "events": events,
            "active_tab": tab,
        },
    )


@router.get("/dashboard/jobs", response_class=HTMLResponse)
def jobs_page():
    return RedirectResponse(url="/dashboard/operations?tab=jobs", status_code=status.HTTP_302_FOUND)


@router.get("/dashboard/audit", response_class=HTMLResponse)
def audit_page():
    return RedirectResponse(url="/dashboard/operations?tab=audit", status_code=status.HTTP_302_FOUND)


@router.post("/dashboard/assignments/{assignment_id}/approve")
def dashboard_approve_assignment(
    assignment_id: int,
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        return RedirectResponse(url="/dashboard/login")

    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        return RedirectResponse(url="/dashboard?error=Assignment+not+found", status_code=status.HTTP_302_FOUND)

    assignment.status = "COMPLETED"

    # Mark all items under this assignment as VERIFIED_COMPLETE
    db.query(AssignmentItem).filter(AssignmentItem.assignment_id == assignment.id).update(
        {AssignmentItem.item_state: "VERIFIED_COMPLETE", AssignmentItem.error: None},
        synchronize_session=False,
    )

    # Approve all background declarations under this assignment
    db.query(BackgroundDeclaration).filter(BackgroundDeclaration.assignment_id == assignment.id).update(
        {
            BackgroundDeclaration.approval_state: "APPROVED",
            BackgroundDeclaration.approved_by: current_user.id,
            BackgroundDeclaration.approved_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )

    claims_deleted = (
        db.query(ActiveImageClaim)
        .filter(ActiveImageClaim.assignment_id == assignment.id)
        .delete()
    )

    # Consolidate all annotated & non-annotated images + sidecar JSONs back to the main project directory
    try:
        from controller.app.assignment_service import consolidate_assignment_to_main_directory
        consolidate_assignment_to_main_directory(db, assignment)
    except Exception as exc:
        logger.warning(f"Error consolidating assignment {assignment.id} files to main directory: {exc}")

    db.add(
        AuditEvent(
            user_id=current_user.id,
            action="ASSIGNMENT_COMPLETION_APPROVED",
            resource_type="ASSIGNMENT",
            resource_id=assignment.id,
            details=f"Master {current_user.username} approved completion of complete assignment #{assignment.id}. All items verified complete, {claims_deleted} active claims released, and files consolidated to main folder.",
        )
    )
    db.commit()
    return RedirectResponse(
        url=f"/dashboard?notice=Complete+Assignment+%23{assignment_id}+approved!+All+images+and+annotations+consolidated+into+main+directory.",
        status_code=status.HTTP_302_FOUND,
    )


@router.post("/dashboard/assignments/{assignment_id}/deny")
def dashboard_deny_assignment(
    assignment_id: int,
    request: Request,
    reason: Optional[str] = Form(None),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user or current_user.role not in ["ADMIN", "MANAGER"]:
        return RedirectResponse(url="/dashboard/login")

    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    if not assignment:
        return RedirectResponse(url="/dashboard?error=Assignment+not+found", status_code=status.HTTP_302_FOUND)

    rework_reason = reason.strip() if reason and reason.strip() else "Supervisor denied completion and requested rework."
    assignment.status = "IN_PROGRESS"

    # Reject background declarations
    db.query(BackgroundDeclaration).filter(BackgroundDeclaration.assignment_id == assignment.id).update(
        {
            BackgroundDeclaration.approval_state: "REJECTED",
            BackgroundDeclaration.approved_by: current_user.id,
        },
        synchronize_session=False,
    )

    # Return background/unverified items back to annotating
    db.query(AssignmentItem).filter(
        AssignmentItem.assignment_id == assignment.id,
        AssignmentItem.item_state.in_(["BACKGROUND_DECLARED", "BACKGROUND_REVIEW"]),
    ).update(
        {AssignmentItem.item_state: "ANNOTATING", AssignmentItem.error: f"Master rework: {rework_reason}"},
        synchronize_session=False,
    )

    db.add(
        AuditEvent(
            user_id=current_user.id,
            action="ASSIGNMENT_COMPLETION_DENIED",
            resource_type="ASSIGNMENT",
            resource_id=assignment.id,
            details=f"Master {current_user.username} denied completion of assignment #{assignment.id}: {rework_reason}",
        )
    )
    db.commit()
    return RedirectResponse(
        url=f"/dashboard?notice=Assignment+%23{assignment_id}+completion+denied.+Task+returned+to+In+Progress+for+rework.",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/api/v1/notifications")
def get_notifications_api(
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if not current_user:
        return {"total_count": 0, "pending_completions": [], "pending_reviews": []}

    pending_comp_query = (
        db.query(Assignment, Dataset, User)
        .join(Dataset, Assignment.dataset_id == Dataset.id)
        .join(User, Assignment.annotator_id == User.id)
        .filter(Assignment.status == "SUBMITTED_FOR_REVIEW")
    )
    if current_user.role == "ANNOTATOR":
        pending_comp_query = pending_comp_query.filter(Assignment.annotator_id == current_user.id)

    pending_comps = []
    for a, ds, u in pending_comp_query.all():
        bg_cnt = (
            db.query(func.count(AssignmentItem.id))
            .filter(
                AssignmentItem.assignment_id == a.id,
                AssignmentItem.item_state.in_(["BACKGROUND_DECLARED", "BACKGROUND_REVIEW", "BACKGROUND_APPROVED"]),
            )
            .scalar() or 0
        )
        obj_cnt = (
            db.query(func.count(AssignmentItem.id))
            .filter(
                AssignmentItem.assignment_id == a.id,
                AssignmentItem.item_state.in_(["VERIFIED_COMPLETE", "UPLOAD_PENDING"]),
            )
            .scalar() or 0
        )
        pending_comps.append({
            "assignment_id": a.id,
            "project_title": ds.title or ds.name,
            "annotator_name": u.username,
            "allocated_count": a.allocated_count,
            "object_count": obj_cnt,
            "background_count": bg_cnt,
            "completed_count": bg_cnt + obj_cnt,
            "submitted_at": a.updated_at.strftime("%Y-%m-%d %H:%M") if a.updated_at else "",
        })

    reviews = []
    if current_user.role in ["ADMIN", "MANAGER"]:
        rev_items = (
            db.query(AssignmentItem, DatasetFile, Assignment)
            .join(DatasetFile, AssignmentItem.file_id == DatasetFile.id)
            .join(Assignment, AssignmentItem.assignment_id == Assignment.id)
            .filter(AssignmentItem.item_state.in_(["FAILED", "CONFLICT"]))
            .limit(50)
            .all()
        )
        for itm, f, assign in rev_items:
            reviews.append({
                "item_id": itm.id,
                "assignment_id": assign.id,
                "file_path": f.normalized_path,
                "state": itm.item_state,
                "error": itm.error,
            })

    total_count = len(pending_comps) + len(reviews)
    return {
        "total_count": total_count,
        "pending_completions": pending_comps,
        "pending_reviews": reviews,
    }


