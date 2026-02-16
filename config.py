"""
Configuration module with validation and environment-specific settings.
"""
import os
import sys

# Ensure the current directory is in the path (fixes Windows multiprocessing issues)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from dotenv import load_dotenv
from typing import Optional

load_dotenv()

class Config:
    """Base configuration."""
    
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY")
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.getenv("SQLALCHEMY_DATABASE_URI")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 10,
        "pool_recycle": 300,
        "pool_pre_ping": True,
        "max_overflow": 20,
    }
    
    # Flask-Admin
    FLASK_ADMIN_SWATCH = "cerulean"
    
    # Salla
    SALLA_SECRET = os.getenv("SALLA_SECRET")
    
    # Blocked events (comma-separated in env)
    BLOCKED_EVENTS = [
        e.strip() for e in os.getenv("BLOCKED_EVENTS", "").split(",") if e.strip()
    ]
    
    # Admin
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "P@123")
    
    # Webhook forwarding
    WEBHOOK_TIMEOUT = int(os.getenv("WEBHOOK_TIMEOUT", "30"))
    WEBHOOK_MAX_RETRIES = int(os.getenv("WEBHOOK_MAX_RETRIES", "5"))
    WEBHOOK_RETRY_BACKOFF = int(os.getenv("WEBHOOK_RETRY_BACKOFF", "60"))
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR = os.getenv("LOG_DIR", "logs")
    
    # Email/SMTP settings
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "noreply@fsolutions.sa")
    SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "FSolutions - Salla Integration")
    
    # Support team notification email settings
    SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@fsolutions.sa")
    SEND_SUPPORT_NOTIFICATIONS = os.getenv("SEND_SUPPORT_NOTIFICATIONS", "true").lower() == "true"

class CeleryConfig:
    """Celery-specific configuration."""
    
    # Convert PostgreSQL URI to SQLAlchemy transport format
    _db_uri = Config.SQLALCHEMY_DATABASE_URI
    
    # SQLAlchemy transport works but has limitations
    broker_url = _db_uri.replace("postgresql://", "sqla+postgresql://", 1)
    result_backend = _db_uri.replace("postgresql://", "db+postgresql://", 1)
    
    # Task settings
    task_serializer = "json"
    result_serializer = "json"
    accept_content = ["json"]
    timezone = "UTC"
    enable_utc = True
    
    # Reliability settings
    task_acks_late = True  # Acknowledge after task completes
    task_reject_on_worker_lost = True  # Requeue if worker dies
    worker_prefetch_multiplier = 1  # Fair distribution
    
    # Broker transport options - only for Redis, not SQLAlchemy
    # visibility_timeout is not supported by SQLAlchemy transport
    broker_connection_retry_on_startup = True
    
    # Result expiration
    result_expires = 86400  # 24 hours
    
    # Enable multiple queues:
    task_routes = {
        "tasks.forward_webhook": {"queue": "webhooks"},
        "tasks.retry_failed_webhooks": {"queue": "retries"},
    }


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True
    SQLALCHEMY_ECHO = True


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    SQLALCHEMY_ECHO = False
    
    # Stricter settings for production
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"


def get_config() -> Config:
    """Get configuration based on environment."""
    env = os.getenv("FLASK_ENV", "production")
    configs = {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
    }
    return configs.get(env, DevelopmentConfig)()