import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
import os

# Set environment before loading modules
os.environ["SECRET_KEY"] = "testsecretkeytestsecretkeytestsecretkey"
os.environ["DATABASE_URL"] = "sqlite:///./test_beauty_pim.db"

from app.database import Base, get_db
from app.main import app
from app.models import User
from app.auth import get_password_hash

# Create test DB
TEST_DATABASE_URL = "sqlite:///./test_beauty_pim.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    # Seed first admin
    admin = User(
        email="admin@test.com",
        hashed_password=get_password_hash("password123"),
        role="admin"
    )
    # Seed a viewer
    viewer = User(
        email="viewer@test.com",
        hashed_password=get_password_hash("password123"),
        role="viewer"
    )
    db.add(admin)
    db.add(viewer)
    db.commit()
    db.close()
    yield
    Base.metadata.drop_all(bind=engine)
    # Remove file
    if os.path.exists("./test_beauty_pim.db"):
        os.remove("./test_beauty_pim.db")

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
