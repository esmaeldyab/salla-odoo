import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:")
os.environ.setdefault("SALLA_SECRET", "test-salla-secret")

# The production engine options (pool_size, max_overflow) are PostgreSQL-only
# and SQLite's driver rejects them. Clear them before importing app, which
# builds an application at module scope.
from config import Config

Config.SQLALCHEMY_ENGINE_OPTIONS = {}

from app import create_app
from models import db as _db, Merchant


@pytest.fixture
def app():
    application = create_app()
    application.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def merchant(db):
    """An active merchant with tokens and an API key."""
    m = Merchant(
        merchant_id="1111111111",
        name="Test Store",
        email="test@example.com",
        odoo_url="https://store-a.example.com/salla/webhook/orders",
        active=True,
    )
    m.update_tokens("access-aaa", "refresh-aaa")
    m.generate_api_key()
    db.session.add(m)
    db.session.commit()
    return m
