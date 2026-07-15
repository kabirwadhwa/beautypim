from datetime import datetime, timedelta
import uuid
from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.config import settings

# Setup password context
import logging
import threading

logger = logging.getLogger("app.auth")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/token")

# Lockout tracker: normalized_email -> { "attempts": int, "locked_until": Optional[datetime] }
_lockout_data = {}
_lockout_lock = threading.Lock()

def normalize_email(email: str) -> str:
    if not email:
        return ""
    return email.strip().lower()

def validate_password_strength(password: str) -> None:
    if not password or len(password) < 12:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 12 characters long."
        )

def record_login_attempt(email: str, success: bool) -> None:
    norm_email = normalize_email(email)
    with _lockout_lock:
        data = _lockout_data.setdefault(norm_email, {"attempts": 0, "locked_until": None})
        if success:
            data["attempts"] = 0
            data["locked_until"] = None
        else:
            data["attempts"] += 1
            if data["attempts"] >= 5:
                data["locked_until"] = datetime.utcnow() + timedelta(minutes=15)
                logger.warning(f"Account locked out: {norm_email} for 15 minutes due to 5 login failures.")

def check_login_lockout(email: str) -> None:
    norm_email = normalize_email(email)
    with _lockout_lock:
        data = _lockout_data.get(norm_email)
        if data and data["locked_until"]:
            if datetime.utcnow() < data["locked_until"]:
                remaining = int((data["locked_until"] - datetime.utcnow()).total_seconds())
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Account locked out due to multiple failed attempts. Retry in {remaining} seconds."
                )
            else:
                data["locked_until"] = None
                data["attempts"] = 0

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user

class RoleChecker:
    def __init__(self, allowed_roles: List[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not authorized to access this resource. Required: {self.allowed_roles}"
            )
        return current_user

# Common injection dependencies
require_admin = RoleChecker(["admin"])
require_editor_or_admin = RoleChecker(["admin", "editor"])
require_viewer_or_above = RoleChecker(["admin", "editor", "viewer"])

def log_audit_event(
    db: Session,
    entity_type: str,
    entity_id: uuid.UUID,
    display_label: Optional[str],
    action: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    changed: Optional[dict] = None,
    user_id: Optional[uuid.UUID] = None,
    actor_type: str = "user",
    reason: Optional[str] = None,
    request_id: Optional[str] = None
):
    from app.models import AuditLog
    audit = AuditLog(
        id=uuid.uuid4(),
        entity_type=entity_type,
        entity_id=entity_id,
        entity_display_label=display_label[:255] if display_label else None,
        user_id=user_id,
        actor_type=actor_type,
        action=action,
        before_snapshot=before,
        after_snapshot=after,
        changed_fields=changed or {},
        reason=reason,
        request_id=request_id
    )
    db.add(audit)
    db.flush()
    return audit
