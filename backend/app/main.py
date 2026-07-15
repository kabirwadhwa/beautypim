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

@asynccontextmanager
async def lifespan(app: FastAPI):
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
