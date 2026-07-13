import os
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    PROJECT_NAME: str = "Beauty PIM Backend"
    API_V1_STR: str = "/api"
    
    # Database
    # Default to sqlite locally, switchable to PostgreSQL via env variable
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./beauty_pim.db")
    
    # Security
    SECRET_KEY: str = os.getenv("SECRET_KEY", "beauty_pim_super_secret_key_change_in_production")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    
    # Deployment environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
    
    # Account Bootstrap Options
    ALLOW_INITIAL_ADMIN_BOOTSTRAP: bool = os.getenv("ALLOW_INITIAL_ADMIN_BOOTSTRAP", "false").lower() in ("true", "1")
    INITIAL_ADMIN_BOOTSTRAP_TOKEN: Optional[str] = os.getenv("INITIAL_ADMIN_BOOTSTRAP_TOKEN", None)
    
    # Rate Limits (hits/minute)
    RATE_LIMIT_LOGIN: str = os.getenv("RATE_LIMIT_LOGIN", "20/minute")
    RATE_LIMIT_UPLOADS: str = os.getenv("RATE_LIMIT_UPLOADS", "10/minute")
    RATE_LIMIT_PROCESS: str = os.getenv("RATE_LIMIT_PROCESS", "5/minute")
    
    # Webhook SSRF Options
    WEBHOOK_ALLOWED_DOMAINS: Optional[str] = os.getenv("WEBHOOK_ALLOWED_DOMAINS", None)
    
    def __init__(self, **values):
        super().__init__(**values)
        is_prod = self.ENVIRONMENT == "production"
        if is_prod:
            defaults = [
                "beauty_pim_super_secret_key_change_in_production",
                "replace_this_with_a_secure_random_string_for_production_use"
            ]
            if not self.SECRET_KEY or self.SECRET_KEY in defaults or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "Production SECRET_KEY must be set, not use default keys, and be at least 32 characters."
                )
    
    # Gemini AI API
    GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY", None)
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    GEMINI_MODEL_VERSION: str = "2.5"
    
    # OpenAI API
    OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY", None)
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    
    PROMPT_VERSION: str = "1.0"
    SCHEMA_VERSION: str = "1.0"
    
    # AI Cost and Job Processing Controls
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "5"))
    MAX_CONCURRENCY: int = int(os.getenv("MAX_CONCURRENCY", "3"))
    MAX_JOB_COST_LIMIT: float = float(os.getenv("MAX_JOB_COST_LIMIT", "50.0"))
    
    # Ingestion Constraints
    MAX_UPLOAD_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB
    ALLOWED_EXTENSIONS: set = {"csv", "json", "xlsx"}
    
    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    
    class Config:
        case_sensitive = True

settings = Settings()
