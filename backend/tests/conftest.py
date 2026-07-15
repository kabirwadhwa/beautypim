import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import os
from alembic.config import Config
from alembic import command

# Set environment before loading modules
os.environ.setdefault("SECRET_KEY", "testsecretkeytestsecretkeytestsecretkey")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_beauty_pim.db")
os.environ["ENVIRONMENT"] = "testing"

from app.database import Base, get_db
from app.main import app
from app.models import User
from app.auth import get_password_hash

# Create test DB
TEST_DATABASE_URL = os.environ["DATABASE_URL"]
connect_args = {}
if TEST_DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine = create_engine(TEST_DATABASE_URL, connect_args=connect_args)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    # Run migrations using Alembic instead of create_all
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(alembic_cfg, "head")

    db = TestingSessionLocal()
    # Seed first admin
    admin = User(
        email="admin@test.com",
        hashed_password=get_password_hash("securepassword123"),
        role="admin"
    )
    # Seed a viewer
    viewer = User(
        email="viewer@test.com",
        hashed_password=get_password_hash("securepassword123"),
        role="viewer"
    )
    # Seed an editor
    editor = User(
        email="editor@test.com",
        hashed_password=get_password_hash("securepassword123"),
        role="editor"
    )
    db.add(admin)
    db.add(viewer)
    db.add(editor)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    # Remove file if SQLite
    if TEST_DATABASE_URL.startswith("sqlite"):
        db_path = TEST_DATABASE_URL.replace("sqlite:///", "")
        if os.path.exists(db_path):
            try:
                os.remove(db_path)
            except Exception:
                pass

@pytest.fixture
def db():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()

@pytest.fixture
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()

@pytest.fixture(autouse=True)
def clear_limiter():
    from app.limiter import limiter
    limiter.history.clear()
