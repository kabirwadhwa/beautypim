import sys
from app.database import SessionLocal, Base, engine
from app.models import User
from app.auth import get_password_hash

ALLOWED_ROLES = {"admin", "editor", "viewer"}


def create_user(email, password, role="admin"):
    if role not in ALLOWED_ROLES:
        print(f"Error: Role must be one of: {', '.join(sorted(ALLOWED_ROLES))}.")
        sys.exit(1)

    db = SessionLocal()
    try:
        # Auto-create tables if not exists
        Base.metadata.create_all(bind=engine)

        existing = db.query(User).filter(User.email == email).first()
        if existing:
            print(f"Error: Email {email} is already registered.")
            sys.exit(1)

        user = User(
            email=email,
            hashed_password=get_password_hash(password),
            role=role
        )
        db.add(user)
        db.commit()
        print(f"Successfully seeded user: {email} (Role: {role})")
    except Exception as e:
        print(f"Error creating admin: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 -m app.commands.create_admin <email> <password> [role]")
        sys.exit(1)
    create_user(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "admin")
