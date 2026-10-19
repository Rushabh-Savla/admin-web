from typing import Optional, List
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from controller.app.database import get_db
from controller.app.models import User
from controller.app.auth import decode_access_token
from controller.app.config import settings

security = HTTPBearer(auto_error=False)


def get_current_user_optional(
    request: Request,
    auth: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[User]:
    token = None
    if auth and auth.credentials:
        token = auth.credentials
    elif settings.SESSION_COOKIE_NAME in request.cookies:
        token = request.cookies.get(settings.SESSION_COOKIE_NAME)

    if not token:
        return None

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        return None

    username = payload["sub"]
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        return None

    return user


def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional),
) -> User:
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required or account is disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_role(allowed_roles: List[str]):
    def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Operation requires one of roles: {', '.join(allowed_roles)}",
            )
        return current_user
    return role_checker

