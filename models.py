
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import Index

db = SQLAlchemy()


class User(db.Model, UserMixin):
    """Admin user model."""
    
    __tablename__ = "users"
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    def update_last_login(self) -> None:
        self.last_login = datetime.utcnow()
    
    def __repr__(self) -> str:
        return f"<User {self.username}>"


class Merchant(db.Model):
    """Merchant configuration for webhook routing."""
    
    __tablename__ = "merchants"
    
    id = db.Column(db.Integer, primary_key=True)
    merchant_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100))
    email = db.Column(db.String(255))
    odoo_url = db.Column(db.String(500), nullable=False)
    active = db.Column(db.Boolean, default=True, index=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Statistics
    webhook_count = db.Column(db.Integer, default=0)
    last_webhook_at = db.Column(db.DateTime)
    last_success_at = db.Column(db.DateTime)
    last_failure_at = db.Column(db.DateTime)
    failure_count = db.Column(db.Integer, default=0)
    
    # Optional per-merchant settings
    timeout_seconds = db.Column(db.Integer, default=30)
    max_retries = db.Column(db.Integer, default=5)
    retry_backof = db.Column(db.Integer, default=1)  # in menutes
    # Custom headers to add (JSON stored as text)
    custom_headers = db.Column(db.Text)
    
    # Logs relationship
    logs = db.relationship("WebhookLog", back_populates="merchant", lazy="dynamic")
    
    # Composite index for common query pattern
    __table_args__ = (
        Index("ix_merchant_active_id", "merchant_id", "active"),
    )

    def increment_webhook_count(self) -> None:
        self.webhook_count = (self.webhook_count or 0) + 1
        self.last_webhook_at = datetime.utcnow()
    
    def record_success(self) -> None:
        self.last_success_at = datetime.utcnow()
    
    def record_failure(self) -> None:
        self.failure_count = (self.failure_count or 0) + 1
        self.last_failure_at = datetime.utcnow()

    def __repr__(self) -> str:
        return f"<Merchant {self.merchant_id} - {self.name}>"


class WebhookLog(db.Model):
    """Log of processed webhooks for debugging and auditing."""
    
    __tablename__ = "webhook_logs"
    
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey("merchants.id"), index=True)
    
    # Event details
    event_type = db.Column(db.String(100), index=True)
    salla_merchant_id = db.Column(db.String(50))
    
    # Status tracking
    status = db.Column(db.String(20), default="pending", index=True)  # pending, forwarded, failed, ignored
    
    # Response details
    odoo_status_code = db.Column(db.Integer)
    odoo_response = db.Column(db.Text)
    error_message = db.Column(db.Text)
    
    # Retry tracking
    retry_count = db.Column(db.Integer, default=0)
    
    # Timestamps
    received_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    forwarded_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    
    # Payload storage for retry capability
    payload = db.Column(db.Text)  # Store original JSON payload
    headers = db.Column(db.Text)  # Store original headers as JSON
    payload_hash = db.Column(db.String(64))  # SHA256 of payload for deduplication
    
    # Relationship
    merchant = db.relationship("Merchant", back_populates="logs")
    
    # Index for cleanup queries
    __table_args__ = (
        Index("ix_webhook_log_cleanup", "received_at", "status"),
    )

    def mark_forwarded(self, status_code: int, response: str = None) -> None:
        self.status = "forwarded"
        self.odoo_status_code = status_code
        self.odoo_response = response[:1000] if response else None  # Truncate
        self.forwarded_at = datetime.utcnow()
        self.completed_at = datetime.utcnow()
    
    def mark_failed(self, error: str) -> None:
        self.status = "failed"
        self.error_message = error[:1000] if error else None
        self.completed_at = datetime.utcnow()
    
    def mark_ignored(self, reason: str) -> None:
        self.status = "ignored"
        self.error_message = reason
        self.completed_at = datetime.utcnow()
    
    def increment_retry(self) -> None:
        self.retry_count = (self.retry_count or 0) + 1

    def __repr__(self) -> str:
        return f"<WebhookLog {self.request_id} - {self.event_type}>"


class BlockedEvent(db.Model):
    """Configurable blocked events (alternative to config file)."""
    
    __tablename__ = "blocked_events"
    
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(100), unique=True, nullable=False, index=True)
    reason = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.String(80))

    def __repr__(self) -> str:
        return f"<BlockedEvent {self.event_type}>"