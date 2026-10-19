from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import User, Device, Assignment, AuditEvent
from controller.app.schemas import (
    UserCreate,
    UserUpdate,
    UserOut,
    DeviceEnrollRequest,
    DeviceOut,
    HeartbeatRequest,
    HeartbeatResponse,
)
from controller.app.auth import hash_password
from controller.app.dependencies import get_current_user, require_role
from controller.app.config import settings

router = APIRouter(prefix="/api/v1", tags=["Users & Devices"])


@router.get("/users", response_model=List[UserOut])
def list_users(
    role: Optional[str] = None,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    query = db.query(User)
    if role:
        query = query.filter(User.role == role.upper())
    return query.order_by(User.id.asc()).all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    req: UserCreate,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    if current_user.role == "MANAGER" and req.role.upper() in {"ADMIN", "MANAGER"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Managers can only create ANNOTATOR users")

    if req.role.upper() not in {"ADMIN", "MANAGER", "ANNOTATOR"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")

    existing = db.query(User).filter(User.username == req.username).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Username '{req.username}' already exists")

    new_user = User(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role.upper(),
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    audit = AuditEvent(
        user_id=current_user.id,
        action="USER_CREATED",
        resource_type="user",
        resource_id=str(new_user.id),
        details={"username": new_user.username, "role": new_user.role},
    )
    db.add(audit)
    db.commit()
    return new_user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    req: UserUpdate,
    current_user: User = Depends(require_role(["ADMIN", "MANAGER"])),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if current_user.role == "MANAGER" and target.role != "ANNOTATOR":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Managers can only modify ANNOTATOR users")

    changes = {}
    if req.is_active is not None:
        target.is_active = req.is_active
        changes["is_active"] = req.is_active
    if req.role is not None:
        if current_user.role == "ADMIN":
            target.role = req.role.upper()
            changes["role"] = target.role
    if req.password:
        target.password_hash = hash_password(req.password)
        changes["password_reset"] = True

    audit = AuditEvent(
        user_id=current_user.id,
        action="USER_UPDATED",
        resource_type="user",
        resource_id=str(target.id),
        details=changes,
    )
    db.add(audit)
    db.commit()
    db.refresh(target)
    return target


@router.post("/devices/enroll", response_model=DeviceOut)
def enroll_device(
    req: DeviceEnrollRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(Device).filter(Device.device_identifier == req.device_identifier).first()
    now_utc = datetime.now(timezone.utc)
    if existing:
        existing.last_seen_at = now_utc
        existing.name = req.name
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        return existing

    import secrets
    token = secrets.token_hex(32)
    device = Device(
        user_id=current_user.id,
        device_identifier=req.device_identifier,
        name=req.name,
        token_hash=hash_password(token),
        is_active=True,
        last_seen_at=now_utc,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(
    req: HeartbeatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    device = db.query(Device).filter(Device.device_identifier == req.device_identifier).first()
    if device:
        device.last_seen_at = now_utc

    expires_at = None
    gen = None

    if req.active_assignment_id:
        assignment = (
            db.query(Assignment)
            .filter(
                Assignment.id == req.active_assignment_id,
                Assignment.annotator_id == current_user.id,
            )
            .first()
        )
        if assignment and assignment.status in {"READY", "IN_PROGRESS"}:
            assignment.status = "IN_PROGRESS"
            expires_at = now_utc + timedelta(seconds=settings.LEASE_DURATION_SECONDS)
            assignment.lease_expires_at = expires_at
            gen = assignment.lease_generation

    db.commit()
    return HeartbeatResponse(
        status="ok",
        lease_expires_at=expires_at,
        lease_generation=gen,
    )

