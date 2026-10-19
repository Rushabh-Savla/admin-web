from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import User, AuditEvent
from controller.app.schemas import LoginRequest, TokenResponse, PasswordChangeRequest, UserOut
from controller.app.auth import verify_password, hash_password, create_access_token
from controller.app.dependencies import get_current_user
from controller.app.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    token = create_access_token(data={"sub": user.username, "role": user.role, "uid": user.id})

    is_https = request.url.scheme == "https"
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=is_https,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )

    audit = AuditEvent(
        user_id=user.id,
        action="LOGIN_SUCCESS",
        resource_type="user",
        resource_id=str(user.id),
        details={"username": user.username},
        ip_address=request.client.host if request.client else None,
    )
    db.add(audit)
    db.commit()

    return TokenResponse(
        access_token=token,
        role=user.role,
        username=user.username,
    )


@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user)):
    response.delete_cookie(key=settings.SESSION_COOKIE_NAME)
    return {"message": "Logged out successfully"}


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/change-password")
def change_password(
    req: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(req.old_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password incorrect")

    if len(req.new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be at least 8 characters")

    current_user.password_hash = hash_password(req.new_password)
    audit = AuditEvent(
        user_id=current_user.id,
        action="PASSWORD_CHANGED",
        resource_type="user",
        resource_id=str(current_user.id),
        details={},
    )
    db.add(audit)
    db.commit()
    return {"message": "Password changed successfully"}

