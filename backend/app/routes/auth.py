from fastapi import APIRouter, Depends, HTTPException, status, Header
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import timedelta
from typing import Optional
from app.database import get_db
from app.models import User
from app.auth import (
    get_password_hash, verify_password, create_access_token, 
    get_current_user, require_admin, normalize_email,
    validate_password_strength, record_login_attempt, check_login_lockout
)
from app.schemas import UserCreate, UserOut, Token, UserLogin
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
    
    record_login_attempt(norm_username, success=True)
    access_token = create_access_token(data={"sub": user.email})
    return {
        "access_token": access_token, 
        "token_type": "bearer",
        "role": user.role
    }

@router.get("/me", response_model=UserOut)
def read_users_me(current_user: User = Depends(get_current_user)):
    return current_user
