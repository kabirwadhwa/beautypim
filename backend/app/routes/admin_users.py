from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import secrets
import hashlib
import uuid
from typing import Optional, List

from app.database import get_db
from app.models import User, UserInvitation, AuditLog
from app.auth import require_admin, normalize_email, log_audit_event
from app.schemas import (
    UserOut, UserInvitationCreate, UserInvitationOut,
    AdminUserUpdateRole, UserInvitationValidateResponse
)
from app.services.email import get_email_service

router = APIRouter(prefix="/admin", tags=["Admin Users"], dependencies=[Depends(require_admin)])

@router.get("/users")
def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None), # active, disabled
    search: Optional[str] = Query(None), # email search
    db: Session = Depends(get_db)
):
    query = db.query(User)
    
    if role:
        query = query.filter(User.role == role)
        
    if status_filter:
        if status_filter == "active":
            query = query.filter(User.is_active == True)
        elif status_filter == "disabled":
            query = query.filter(User.is_active == False)
            
    if search:
        query = query.filter(User.email.contains(search.strip().lower()))
        
    total = query.count()
    offset = (page - 1) * limit
    users = query.order_by(User.created_at.desc()).offset(offset).limit(limit).all()
    
    # Map to custom pagination response
    # Include inviter email if invited
    users_out = []
    for u in users:
        invited_by_email = None
        if u.invited_by_id:
            inviter = db.query(User).filter(User.id == u.invited_by_id).first()
            if inviter:
                invited_by_email = inviter.email
        users_out.append({
            "id": u.id,
            "email": u.email,
            "role": u.role,
            "is_active": u.is_active,
            "last_login_at": u.last_login_at,
            "accepted_invitation_at": u.accepted_invitation_at,
            "disabled_at": u.disabled_at,
            "created_at": u.created_at,
            "invited_by": invited_by_email
        })
        
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "users": users_out
    }

@router.post("/invitations", response_model=UserInvitationOut, status_code=status.HTTP_201_CREATED)
def create_invitation(
    data: UserInvitationCreate,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    norm_email = normalize_email(data.email)
    
    if data.role not in ["admin", "editor", "viewer"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role selected."
        )
        
    # Check if active user already exists
    existing_user = db.query(User).filter(User.email == norm_email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email address already exists."
        )
        
    # Check if a pending invitation exists
    existing_pending = db.query(UserInvitation).filter(
        UserInvitation.email == norm_email,
        UserInvitation.status == "pending"
    ).first()
    if existing_pending:
        # Check if expired
        expires_naive = existing_pending.expires_at.replace(tzinfo=None) if existing_pending.expires_at.tzinfo else existing_pending.expires_at
        if expires_naive > datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A pending invitation already exists for this email. Please use resend/revoke."
            )
        else:
            # Mark it expired dynamically
            existing_pending.status = "expired"
            db.add(existing_pending)
            db.commit()
            
    # Generate token
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
    expires_at = datetime.utcnow() + timedelta(hours=72)
    
    invitation = None
    try:
        # Transaction 1: create invitation and log audit event
        invitation = UserInvitation(
            id=uuid.uuid4(),
            email=norm_email,
            role=data.role,
            token_hash=token_hash,
            invited_by_id=current_admin.id,
            status="pending",
            expires_at=expires_at,
            resend_count=0
        )
        db.add(invitation)
        db.flush()
        
        log_audit_event(
            db=db,
            entity_type="UserInvitation",
            entity_id=invitation.id,
            display_label=invitation.email,
            action="invitation_created",
            before=None,
            after={"email": invitation.email, "role": invitation.role, "status": "pending"},
            changed={"status": "pending"},
            user_id=current_admin.id
        )
        
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create invitation in database."
        )
        
    # Send email outside of the transaction
    email_service = get_email_service()
    delivery_status = "sent"
    delivery_error = None
    try:
        email_service.send_invitation(
            to_email=invitation.email,
            role=invitation.role,
            raw_token=raw_token,
            expires_at=expires_at,
            inviter_email=current_admin.email
        )
    except Exception as e:
        delivery_status = "failed"
        delivery_error = str(e)
        
    # Transaction 2: Update delivery status
    try:
        invitation_db = db.query(UserInvitation).filter(UserInvitation.id == invitation.id).first()
        if invitation_db:
            invitation_db.email_delivery_status = delivery_status
            invitation_db.email_delivery_error = delivery_error
            db.add(invitation_db)
            db.commit()
            db.refresh(invitation_db)
            return invitation_db
    except Exception:
        db.rollback()
        
    return invitation

@router.get("/invitations")
def list_invitations(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None), # pending, accepted, revoked, expired
    db: Session = Depends(get_db)
):
    query = db.query(UserInvitation)
    
    if status_filter:
        query = query.filter(UserInvitation.status == status_filter)
        
    total = query.count()
    offset = (page - 1) * limit
    invitations = query.order_by(UserInvitation.created_at.desc()).offset(offset).limit(limit).all()
    
    # Map to custom response with inviter name
    invites_out = []
    for inv in invitations:
        inviter_email = None
        if inv.invited_by_id:
            inviter = db.query(User).filter(User.id == inv.invited_by_id).first()
            if inviter:
                inviter_email = inviter.email
        invites_out.append({
            "id": inv.id,
            "email": inv.email,
            "role": inv.role,
            "status": inv.status,
            "expires_at": inv.expires_at,
            "last_sent_at": inv.last_sent_at,
            "resend_count": inv.resend_count,
            "email_delivery_status": inv.email_delivery_status,
            "email_delivery_error": inv.email_delivery_error,
            "created_at": inv.created_at,
            "invited_by": inviter_email
        })
        
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "invitations": invites_out
    }

@router.post("/invitations/{invitation_id}/resend", response_model=UserInvitationOut)
def resend_invitation(
    invitation_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    # Lock invitation and verify state
    try:
        invitation = db.query(UserInvitation).filter(
            UserInvitation.id == invitation_id
        ).with_for_update().first()
        
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invitation not found."
            )
            
        if invitation.status not in ["pending", "expired"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot resend invitation with status: {invitation.status}."
            )
            
        # Generate new token
        raw_token = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
        expires_at = datetime.utcnow() + timedelta(hours=72)
        
        before_state = {"status": invitation.status, "token_hash": invitation.token_hash}
        
        invitation.token_hash = new_hash
        invitation.status = "pending"
        invitation.expires_at = expires_at
        invitation.resend_count += 1
        invitation.last_sent_at = datetime.utcnow()
        
        db.add(invitation)
        
        log_audit_event(
            db=db,
            entity_type="UserInvitation",
            entity_id=invitation.id,
            display_label=invitation.email,
            action="invitation_resent",
            before=before_state,
            after={"status": "pending", "token_hash": new_hash, "resend_count": invitation.resend_count},
            changed={"token_hash": new_hash, "status": "pending"},
            user_id=current_admin.id
        )
        
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resend invitation in database."
        )
        
    # Send email outside of the transaction
    email_service = get_email_service()
    delivery_status = "sent"
    delivery_error = None
    try:
        email_service.send_invitation(
            to_email=invitation.email,
            role=invitation.role,
            raw_token=raw_token,
            expires_at=expires_at,
            inviter_email=current_admin.email
        )
    except Exception as e:
        delivery_status = "failed"
        delivery_error = str(e)
        
    # Transaction 2: Update delivery status
    try:
        invitation_db = db.query(UserInvitation).filter(UserInvitation.id == invitation.id).first()
        if invitation_db:
            invitation_db.email_delivery_status = delivery_status
            invitation_db.email_delivery_error = delivery_error
            db.add(invitation_db)
            db.commit()
            db.refresh(invitation_db)
            return invitation_db
    except Exception:
        db.rollback()
        
    return invitation

@router.post("/invitations/{invitation_id}/revoke", response_model=UserInvitationOut)
def revoke_invitation(
    invitation_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    try:
        invitation = db.query(UserInvitation).filter(
            UserInvitation.id == invitation_id
        ).with_for_update().first()
        
        if not invitation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Invitation not found."
            )
            
        if invitation.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only pending invitations may be revoked."
            )
            
        before_state = {"status": invitation.status}
        invitation.status = "revoked"
        invitation.revoked_at = datetime.utcnow()
        db.add(invitation)
        
        log_audit_event(
            db=db,
            entity_type="UserInvitation",
            entity_id=invitation.id,
            display_label=invitation.email,
            action="invitation_revoked",
            before=before_state,
            after={"status": "revoked"},
            changed={"status": "revoked"},
            user_id=current_admin.id
        )
        
        db.commit()
        db.refresh(invitation)
        return invitation
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to revoke invitation."
        )

@router.patch("/users/{user_id}/role")
def change_user_role(
    user_id: uuid.UUID,
    data: AdminUserUpdateRole,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    if data.role not in ["admin", "editor", "viewer"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role specified."
        )
        
    try:
        # Lock target user
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found."
            )
            
        # Lock active admins to avoid race condition
        active_admins = db.query(User).filter(
            User.role == "admin",
            User.is_active == True
        ).with_for_update().all()
        
        # Verify final admin security rule
        if user.role == "admin" and user.is_active and data.role != "admin":
            if len(active_admins) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot downgrade the final active administrator."
                )
                
        before_role = user.role
        user.role = data.role
        db.add(user)
        
        log_audit_event(
            db=db,
            entity_type="User",
            entity_id=user.id,
            display_label=user.email,
            action="user_role_changed",
            before={"role": before_role},
            after={"role": user.role},
            changed={"role": user.role},
            user_id=current_admin.id
        )
        
        db.commit()
        return {"message": "User role updated successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change user role."
        )

@router.post("/users/{user_id}/disable")
def disable_user(
    user_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    try:
        # Lock target user
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found."
            )
            
        # Lock active admins
        active_admins = db.query(User).filter(
            User.role == "admin",
            User.is_active == True
        ).with_for_update().all()
        
        if user.role == "admin" and user.is_active:
            if len(active_admins) <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot disable the final active administrator."
                )
                
        if not user.is_active:
            return {"message": "User is already disabled"}
            
        user.is_active = False
        user.disabled_at = datetime.utcnow()
        db.add(user)
        
        log_audit_event(
            db=db,
            entity_type="User",
            entity_id=user.id,
            display_label=user.email,
            action="user_disabled",
            before={"is_active": True},
            after={"is_active": False},
            changed={"is_active": False},
            user_id=current_admin.id
        )
        
        db.commit()
        return {"message": "User disabled successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable user."
        )

@router.post("/users/{user_id}/enable")
def enable_user(
    user_id: uuid.UUID,
    current_admin: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    try:
        user = db.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found."
            )
            
        if user.is_active:
            return {"message": "User is already active"}
            
        user.is_active = True
        user.disabled_at = None
        db.add(user)
        
        log_audit_event(
            db=db,
            entity_type="User",
            entity_id=user.id,
            display_label=user.email,
            action="user_enabled",
            before={"is_active": False},
            after={"is_active": True},
            changed={"is_active": True},
            user_id=current_admin.id
        )
        
        db.commit()
        return {"message": "User enabled successfully"}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enable user."
        )
