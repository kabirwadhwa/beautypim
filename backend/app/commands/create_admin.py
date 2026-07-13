import sys
from app.database import SessionLocal, Base, engine
from app.models import User
from app.auth import get_password_hash

def create_admin(email, password):
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
            role="admin"
        )
        db.add(user)
        db.commit()
        print(f"Successfully seeded admin user: {email} (Role: admin)")
    except Exception as e:
        print(f"Error creating admin: {e}")
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 -m app.commands.create_admin <email> <password>")
        sys.exit(1)
    create_admin(sys.argv[1], sys.argv[2])
