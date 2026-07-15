import logging
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db, engine, Base
from app.routes import auth, feeds, products, exports, admin_users
from app.worker import recover_unfinished_jobs

# Structured logging configuration
class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

import sys

logger = logging.getLogger()
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)
logger.setLevel(settings.LOG_LEVEL)

# Redirect Uvicorn logging handlers to root logger stdout to prevent stderr redirection on Railway
for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    l = logging.getLogger(logger_name)
    l.handlers = []
    l.propagate = True

def run_migrations():
    import os
    from alembic.config import Config
    from alembic import command
    from sqlalchemy import inspect
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ini_path = os.path.join(base_dir, "alembic.ini")
        alembic_cfg = Config(ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        
        # Check if users table exists but alembic_version doesn't (existing DB)
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        if "users" in tables and "alembic_version" not in tables:
            logger.info("Existing database detected. Stamping to 57b63617a0af before upgrade...")
            command.stamp(alembic_cfg, "57b63617a0af")
            
        logger.info("Running database migrations on startup...")
        command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations completed successfully.")
    except Exception as e:
        logger.error(f"Failed to run database migrations: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run migrations programmatically
    run_migrations()
    
    # Create tables automatically (zero-config SQLite/PostgreSQL boot)
    Base.metadata.create_all(bind=engine)
    
    # Recover jobs on startup
    recover_unfinished_jobs()
    yield

app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc"
)



# CORS configurations
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, lock down based on config
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire Routes
app.include_router(auth.router, prefix=settings.API_V1_STR)
app.include_router(admin_users.router, prefix=settings.API_V1_STR)
app.include_router(feeds.router, prefix=settings.API_V1_STR)
app.include_router(products.router, prefix=settings.API_V1_STR)
app.include_router(exports.router, prefix=settings.API_V1_STR)

@app.get("/health", tags=["System Controls"])
def health_check():
    return {"status": "healthy"}

@app.get("/debug-db")
def debug_db(db: Session = Depends(get_db)):
    import traceback
    import os
    from sqlalchemy import inspect
    from alembic.config import Config
    from alembic import command
    
    migration_error = None
    migration_trace = None
    try:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ini_path = os.path.join(base_dir, "alembic.ini")
        alembic_cfg = Config(ini_path)
        alembic_cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        
        # Check if users table exists but alembic_version doesn't (existing DB)
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        if "users" in tables and "alembic_version" not in tables:
            command.stamp(alembic_cfg, "57b63617a0af")
            
        command.upgrade(alembic_cfg, "head")
    except Exception as ex:
        migration_error = str(ex)
        migration_trace = traceback.format_exc()
        
    try:
        inspector = inspect(engine)
        user_columns = [col["name"] for col in inspector.get_columns("users")]
        user_count = db.execute(text("SELECT count(*) FROM users")).scalar()
        inv_exists = "user_invitations" in inspector.get_table_names()
        
        return {
            "success": True,
            "database_url": settings.DATABASE_URL.split("@")[-1] if "@" in settings.DATABASE_URL else "sqlite",
            "users_table_columns": user_columns,
            "user_count": user_count,
            "user_invitations_table_exists": inv_exists,
            "migration_error": migration_error,
            "migration_trace": migration_trace
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc(),
            "migration_error": migration_error,
            "migration_trace": migration_trace
        }

@app.get("/ready", tags=["System Controls"])
def readiness_check(db: Session = Depends(get_db)):
    try:
        # Run simple query to check DB availability
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database not ready: {str(e)}"
        )
