import pytest
from sqlalchemy import create_engine, text
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

    if TEST_DATABASE_URL.startswith("sqlite"):
        _seed_default_users(TestingSessionLocal())
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

def _seed_default_users(session):
    try:
        for email, role in (
            ("admin@test.com", "admin"),
            ("viewer@test.com", "viewer"),
            ("editor@test.com", "editor"),
        ):
            session.add(User(
                email=email,
                hashed_password=get_password_hash("securepassword123"),
                role=role,
                is_active=True,
            ))
        session.commit()
    finally:
        session.close()


def _reset_postgres_database():
    """Give tests and worker-created sessions the same committed database state."""
    table_names = [
        table.name for table in reversed(Base.metadata.sorted_tables)
        if table.name != "alembic_version"
    ]
    if table_names:
        quoted = ", ".join(f'"{name}"' for name in table_names)
        with engine.begin() as connection:
            connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db():
    if TEST_DATABASE_URL.startswith("postgresql"):
        _reset_postgres_database()
        _seed_default_users(TestingSessionLocal())
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()
            _reset_postgres_database()
        return

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
