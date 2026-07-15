import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import SessionLocal
from app.models import User, UserInvitation, AuditLog
from app.auth import get_password_hash

def reset():
    db = SessionLocal()
    try:
        # Delete invitations and audit logs
        db.query(UserInvitation).delete()
        db.query(AuditLog).delete()
        db.query(User).delete()
        db.commit()
        
        # Seed users
        admin = User(
            email="admin@test.com",
            hashed_password=get_password_hash("securepassword123"),
            role="admin",
            is_active=True
        )
        editor = User(
            email="editor@test.com",
            hashed_password=get_password_hash("securepassword123"),
            role="editor",
            is_active=True
        )
        viewer = User(
            email="viewer@test.com",
            hashed_password=get_password_hash("securepassword123"),
            role="viewer",
            is_active=True
        )
        db.add_all([admin, editor, viewer])
        db.commit()
        print("Database reset and seeded successfully.")
    except Exception as e:
        db.rollback()
        print(f"Error resetting database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    reset()
