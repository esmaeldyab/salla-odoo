"""
Celery application factory.

This module creates the Celery instance independently of Flask
to avoid circular import issues.
"""
from celery import Celery
from config import CeleryConfig


def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    celery = Celery("salla_router")
    celery.config_from_object(CeleryConfig)
    return celery


# Global Celery instance
celery_app = create_celery_app()