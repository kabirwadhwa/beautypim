from fastapi import APIRouter, Depends, HTTPException, status, Header
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import timedelta, datetime, timezone
import hashlib
from typing import Optional
from app.database import get_db
from app.models import User, UserInvitation
from app.auth import (
    get_password_hash, verify_password, create_access_token, 
    get_current_user, require_admin, normalize_email,
    validate_password_strength, record_login_attempt, check_login_lockout,
    log_audit_event
)
from app.schemas import (
    UserCreate, UserOut, Token, UserLogin,
    UserInvitationValidate, UserInvitationValidateResponse, UserInvitationAccept
)
from app.config import settings

from app.limiter import rate_limit

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit("register", "10/minute"))])
def register_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
    x_bootstrap_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    norm_email = normalize_email(user_in.email)
    validate_password_strength(user_in.password)
    
    # Prevent concurrent registration races
    if db.bind.dialect.name == "postgresql":
        db.execute(text("LOCK TABLE users IN ACCESS EXCLUSIVE MODE"))
        
    # Check if this is the first user overall (bootstrap admin role)
    first_user = db.query(User).first() is None
    
    if first_user:
        if not settings.ALLOW_INITIAL_ADMIN_BOOTSTRAP:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Initial admin bootstrap is disabled. Use the CLI seed command."
            )
        if not settings.INITIAL_ADMIN_BOOTSTRAP_TOKEN or x_bootstrap_token != settings.INITIAL_ADMIN_BOOTSTRAP_TOKEN:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing initial admin bootstrap token."
            )
        role = "admin"
    else:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated. Only authenticated administrators may create users."
            )
        try:
            token = authorization.split(" ")[1]
            current_user = get_current_user(token=token, db=db)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials."
            )
        if current_user.role != "admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only administrators may create new users."
            )
        role = "viewer"
        
    existing = db.query(User).filter(User.email == norm_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
        
    hashed = get_password_hash(user_in.password)
    user = User(
        email=norm_email,
        hashed_password=hashed,
        role=role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

@router.post("/token", response_model=Token, dependencies=[Depends(rate_limit("login", "RATE_LIMIT_LOGIN"))])
def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    norm_username = normalize_email(form_data.username)
    check_login_lockout(norm_username)
    
    user = db.query(User).filter(User.email == norm_username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        record_login_attempt(norm_username, success=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account is disabled.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    record_login_attempt(norm_username, success=True)
    
    # Update last_login_at
    user.last_login_at = datetime.now(timezone.utc)
    db.add(user)
    db.commit()
    
    access_token = create_access_token(data={"sub": user.email})
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "role": user.role
    }

@router.get("/me", response_model=UserOut)
def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.post("/invitations/validate", response_model=UserInvitationValidateResponse, dependencies=[Depends(rate_limit("validate_invite", "10/minute"))])
def validate_invitation(
    data: UserInvitationValidate,
    db: Session = Depends(get_db)
):
    token_hash = hashlib.sha256(data.token.encode('utf-8')).hexdigest()
    invitation = db.query(UserInvitation).filter(UserInvitation.token_hash == token_hash).first()
    
    # Generic message to prevent disclosure
    generic_error = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid or expired invitation link"
    )
    
    if not invitation or invitation.status != "pending":
        raise generic_error
        
    inv_expires = invitation.expires_at.replace(tzinfo=None) if invitation.expires_at.tzinfo else invitation.expires_at
    if inv_expires < datetime.utcnow():
        raise generic_error
        
    # Prevent validation if user already exists
    norm_email = normalize_email(invitation.email)
    existing = db.query(User).filter(User.email == norm_email).first()
    if existing:
        raise generic_error
        
    return {
        "valid": True,
        "email": invitation.email,
        "role": invitation.role,
        "expires_at": invitation.expires_at
    }

@router.post("/invitations/accept", status_code=status.HTTP_200_OK, dependencies=[Depends(rate_limit("accept_invite", "10/minute"))])
def accept_invitation(
    data: UserInvitationAccept,
    db: Session = Depends(get_db)
):
    if data.password != data.password_confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match"
        )
    validate_password_strength(data.password)
    
    token_hash = hashlib.sha256(data.token.encode('utf-8')).hexdigest()
    
    generic_error = HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Invalid or expired invitation link"
    )
    
    try:
        # Lock the row with SELECT FOR UPDATE
        invitation = db.query(UserInvitation).filter(
            UserInvitation.token_hash == token_hash
        ).with_for_update().first()
        
        if not invitation or invitation.status != "pending":
            raise generic_error
            
        inv_expires = invitation.expires_at.replace(tzinfo=None) if invitation.expires_at.tzinfo else invitation.expires_at
        if inv_expires < datetime.utcnow():
            raise generic_error
            
        # Prevent acceptance if user already exists
        norm_email = normalize_email(invitation.email)
        existing = db.query(User).filter(User.email == norm_email).first()
        if existing:
            raise generic_error
            
        # Create User
        user = User(
            email=norm_email,
            hashed_password=get_password_hash(data.password),
            role=invitation.role,
            is_active=True,
            invited_by_id=invitation.invited_by_id,
            accepted_invitation_at=datetime.utcnow()
        )
        db.add(user)
        db.flush() # Get user.id for audit log
        
        # Mark invitation accepted
        invitation.status = "accepted"
        invitation.accepted_at = datetime.utcnow()
        db.add(invitation)
        
        # Create audit log atomically in the transaction
        log_audit_event(
            db=db,
            entity_type="User",
            entity_id=user.id,
            display_label=user.email,
            action="invitation_accepted",
            before={"status": "pending", "invitation_id": str(invitation.id)},
            after={"status": "accepted", "invitation_id": str(invitation.id), "role": user.role},
            changed={"status": "accepted"},
            user_id=user.id,
            actor_type="invited_user"
        )
        
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to accept invitation due to a server error."
        )
        
    return {"message": "Invitation accepted successfully"}
