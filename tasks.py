"""
Celery tasks for webhook processing.

Features:
- Exponential backoff with jitter
- Connection pooling via requests.Session
- Proper Flask app context handling
- Comprehensive logging and tracking
- Dead letter handling for permanent failures
"""
import os
import sys

# Ensure the current directory is in the path (fixes Windows multiprocessing issues)
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import json
import hmac
import hashlib
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from uuid import uuid4

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from celery import Celery

from config import Config, CeleryConfig

# ====================== CELERY APP INITIALIZATION ======================

def create_celery_app() -> Celery:
    """Create and configure the Celery application."""
    celery = Celery("salla_router")
    celery.config_from_object(CeleryConfig)
    return celery

# Global Celery instance
celery_app = create_celery_app()

# ====================== LOGGING SETUP ======================

# Configure logging
os.makedirs(Config.LOG_DIR, exist_ok=True)

logger = logging.getLogger("salla_router")
logger.setLevel(getattr(logging, Config.LOG_LEVEL))

file_handler = logging.FileHandler(f"{Config.LOG_DIR}/webhook.log")
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logger.addHandler(file_handler)

# Also log to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(console_handler)


# ====================== HTTP SESSION WITH RETRY ======================

def create_http_session() -> requests.Session:
    """
    Create a requests session with connection pooling and automatic retries.
    
    This is more efficient than creating new connections for each request.
    """
    session = requests.Session()
    
    # Configure retry strategy for transient errors
    retry_strategy = Retry(
        total=0,  # We handle retries via Celery, not requests
        backoff_factor=0,
        status_forcelist=[],  # Don't retry at requests level
    )
    
    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=50,
        max_retries=retry_strategy,
    )
    
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session


# Global session for connection pooling (per worker)
_http_session: Optional[requests.Session] = None


def get_http_session() -> requests.Session:
    """Get or create the HTTP session."""
    global _http_session
    if _http_session is None:
        _http_session = create_http_session()
    return _http_session


# ====================== HELPER FUNCTIONS ======================

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify Salla webhook signature using HMAC-SHA256.
    
    Args:
        payload: Raw request body bytes
        signature: X-Salla-Signature header value
        secret: Salla webhook secret
        
    Returns:
        True if signature is valid
    """
    if not signature or not secret:
        return False
    
    expected = hmac.new(
        secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(expected.lower(), signature.lower())


def compute_payload_hash(payload: str) -> str:
    """Compute SHA256 hash of payload for deduplication."""
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def parse_payload(raw_payload: str) -> Tuple[Dict[str, Any], str, str]:
    """
    Parse webhook payload and extract key fields.
    
    Returns:
        Tuple of (payload_dict, event_type, merchant_id)
    """
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        payload = {}
    
    event = payload.get("event", "unknown")
    merchant_id = str(payload.get("merchant") or payload.get("store_id", ""))
    
    return payload, event, merchant_id


def calculate_backoff(retry_count: int, base_delay: int = 60) -> int:
    """
    Calculate exponential backoff with jitter.
    
    Formula: min(base_delay * 2^retry_count + random_jitter, max_delay)
    """
    import random
    
    max_delay = 3600  # 1 hour max
    delay = min(base_delay * (2 ** retry_count), max_delay)
    jitter = random.uniform(0, delay * 0.1)  # Up to 10% jitter
    
    return int(delay + jitter)


# ====================== FLASK CONTEXT TASK ======================

class FlaskTask(celery_app.Task):
    """
    Celery task base class that provides Flask application context.
    
    Uses lazy initialization to avoid creating the app on import.
    """
    
    _flask_app = None
    
    @property
    def flask_app(self):
        if self._flask_app is None:
            # Ensure current directory is in path (Windows fix)
            import sys
            import os
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if current_dir not in sys.path:
                sys.path.insert(0, current_dir)
            
            from app import create_app
            self._flask_app = create_app()
        return self._flask_app
    
    def __call__(self, *args, **kwargs):
        with self.flask_app.app_context():
            return self.run(*args, **kwargs)


# Set as default task class
celery_app.Task = FlaskTask


# ====================== CELERY TASKS ======================

@celery_app.task(
    bind=True,
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def forward_webhook(
    self,
    request_id: str,
    merchant_id: str,
    raw_payload: str,
    headers_dict: Dict[str, str],
) -> Dict[str, Any]:
    """
    Asynchronously forward webhook payload to the configured Odoo instance.
    
    Args:
        request_id: Unique identifier for this webhook request
        merchant_id: Salla merchant ID
        raw_payload: Original request body as string
        headers_dict: Headers to forward
        
    Returns:
        Status dictionary with result details
    """
    from models import db, Merchant, WebhookLog
    
    # Parse payload for logging
    payload, event, _ = parse_payload(raw_payload)
    
    # Get or create log entry
    log_entry = WebhookLog.query.filter_by(request_id=request_id).first()
    if not log_entry:
        log_entry = WebhookLog(
            request_id=request_id,
            event_type=event,
            salla_merchant_id=merchant_id,
            payload_hash=compute_payload_hash(raw_payload),
            payload=raw_payload,  # Store for retry capability
            headers=json.dumps(headers_dict),  # Store headers as JSON
        )
        db.session.add(log_entry)
    
    try:
        # Look up merchant configuration
        merchant = Merchant.query.filter_by(
            merchant_id=merchant_id,
            active=True
        ).first()
        
        if not merchant:
            logger.warning(
                f"[{request_id}] No active merchant found for ID: {merchant_id}"
            )
            log_entry.mark_ignored(f"No active merchant: {merchant_id}")
            db.session.commit()
            return {"status": "ignored", "reason": "no_active_merchant"}
        
        # Update log with merchant reference
        log_entry.merchant_id = merchant.id
        
        # Get target URL
        target_url = merchant.odoo_url.rstrip("/")
        timeout = merchant.timeout_seconds or Config.WEBHOOK_TIMEOUT
        max_retries = merchant.max_retries or Config.WEBHOOK_MAX_RETRIES
        
        # Add custom headers if configured
        forward_headers = dict(headers_dict)
        if merchant.custom_headers:
            try:
                custom = json.loads(merchant.custom_headers)
                forward_headers.update(custom)
            except json.JSONDecodeError:
                pass
        
        # Add tracking header
        forward_headers["X-Request-ID"] = request_id
        
        # Forward the webhook
        session = get_http_session()
        
        logger.info(
            f"[{request_id}] Forwarding {event} to {target_url} "
            f"(attempt {self.request.retries + 1})"
        )
        
        # Encode payload back to bytes for HTTP request
        payload_bytes = raw_payload.encode('utf-8')
        
        response = session.post(
            target_url,
            data=payload_bytes,
            headers=forward_headers,
            timeout=timeout,
        )
        
        # Raise for 4xx/5xx status codes
        response.raise_for_status()
        
        # Success!
        merchant.record_success()
        log_entry.mark_forwarded(response.status_code, response.text[:500])
        db.session.commit()
        
        logger.info(
            f"[{request_id}] Successfully forwarded {event} → {target_url} "
            f"(status: {response.status_code})"
        )
        
        return {
            "status": "forwarded",
            "request_id": request_id,
            "odoo_status": response.status_code,
        }
        
    except requests.exceptions.Timeout as exc:
        return handle_retry(
            self, request_id, merchant_id, event, exc,
            "timeout", raw_payload, headers_dict, log_entry
        )
        
    except requests.exceptions.ConnectionError as exc:
        return handle_retry(
            self, request_id, merchant_id, event, exc,
            "connection_error", raw_payload, headers_dict, log_entry
        )
        
    except requests.exceptions.HTTPError as exc:
        # Retry on 5xx and 404 (endpoint might not be ready)
        # Fail permanently on other 4xx (auth issues, bad requests)
        if exc.response is not None:
            status_code = exc.response.status_code
            
            # 404 might be temporary (Odoo restarting, endpoint not ready)
            # 5xx are server errors that might resolve
            if status_code == 404 or status_code >= 500:
                return handle_retry(
                    self, request_id, merchant_id, event, exc,
                    f"http_{status_code}", raw_payload, headers_dict, log_entry
                )
            
            # Other 4xx are permanent failures (401, 403, 400, etc.)
            if 400 <= status_code < 500:
                logger.error(
                    f"[{request_id}] Permanent failure: {event} got "
                    f"{status_code} from Odoo"
                )
                log_entry.mark_failed(f"HTTP {status_code}: {str(exc)}")
                if 'merchant' in dir() and merchant:
                    merchant.record_failure()
                db.session.commit()
                return {
                    "status": "failed",
                    "request_id": request_id,
                    "reason": "client_error",
                    "http_status": status_code,
                }
        
        # Unknown HTTP error - retry
        return handle_retry(
            self, request_id, merchant_id, event, exc,
            "http_error", raw_payload, headers_dict, log_entry
        )
        
    except Exception as exc:
        logger.exception(f"[{request_id}] Unexpected error processing webhook")
        log_entry.mark_failed(str(exc))
        db.session.commit()
        return {
            "status": "error",
            "request_id": request_id,
            "message": str(exc),
        }


def handle_retry(
    task,
    request_id: str,
    merchant_id: str,
    event: str,
    exc: Exception,
    error_type: str,
    raw_payload: bytes,
    headers_dict: Dict[str, str],
    log_entry,
) -> Dict[str, Any]:
    """Handle retry logic with exponential backoff."""
    from models import db, Merchant
    
    max_retries = Config.WEBHOOK_MAX_RETRIES
    retry_backof = Config.WEBHOOK_RETRY_BACKOFF
    current_retry = task.request.retries
    
    # Update log
    log_entry.increment_retry()
    
    # Update merchant failure count
    merchant = Merchant.query.filter_by(merchant_id=merchant_id, active=True).first()
    if merchant:
        max_retries = merchant.max_retries or Config.WEBHOOK_MAX_RETRIES
        retry_backof = merchant.retry_backof or Config.WEBHOOK_RETRY_BACKOFF
        merchant.record_failure()
    
    if current_retry >= max_retries:
        # Max retries exceeded - move to dead letter
        logger.error(
            f"[{request_id}] Max retries ({max_retries}) exceeded for {event}. "
            f"Moving to dead letter queue."
        )
        log_entry.mark_failed(f"Max retries exceeded: {error_type}")
        db.session.commit()
        
        # Optionally dispatch to dead letter task
        handle_dead_letter.delay(request_id, merchant_id, event, str(exc))
        
        return {
            "status": "failed",
            "request_id": request_id,
            "reason": "max_retries_exceeded",
        }
    
    # Calculate backoff
    backoff = retry_backof * 60
    
    logger.warning(
        f"[{request_id}] Retry {current_retry + 1}/{max_retries} for {event} "
        f"({error_type}). Next attempt in {backoff}s."
    )
    
    db.session.commit()
    
    # Schedule retry
    raise task.retry(exc=exc, countdown=backoff)


@celery_app.task(bind=True)
def handle_dead_letter(
    self,
    request_id: str,
    merchant_id: str,
    event: str,
    error: str,
) -> Dict[str, Any]:
    """
    Handle permanently failed webhooks.
    
    This could be extended to:
    - Send alerts to administrators
    - Store in a separate dead letter table
    - Notify external monitoring systems
    """
    logger.error(
        f"[DEAD LETTER] request_id={request_id}, merchant={merchant_id}, "
        f"event={event}, error={error}"
    )
    
    # TODO: Add alerting integration (email, Slack, PagerDuty, etc.)
    
    return {
        "status": "dead_letter",
        "request_id": request_id,
        "merchant_id": merchant_id,
        "event": event,
    }


@celery_app.task(bind=True)
def retry_webhook_by_id(self, log_id: int) -> Dict[str, Any]:
    """
    Retry a failed webhook by its log ID.
    
    Called from the admin panel to manually retry failed webhooks.
    """
    from models import db, Merchant, WebhookLog
    
    log_entry = WebhookLog.query.get(log_id)
    
    if not log_entry:
        logger.warning(f"Retry requested for non-existent log ID: {log_id}")
        return {"status": "error", "reason": "log_not_found"}
    
    if log_entry.status != "failed":
        logger.info(f"Skipping retry for log {log_id} - status is {log_entry.status}")
        return {"status": "skipped", "reason": f"status_is_{log_entry.status}"}
    
    # Check if payload is stored
    if not log_entry.payload:
        logger.error(f"Cannot retry log {log_id} - no payload stored")
        return {"status": "error", "reason": "no_payload_stored"}
    
    # Find the merchant
    merchant = Merchant.query.filter_by(
        merchant_id=log_entry.salla_merchant_id,
        active=True
    ).first()
    
    if not merchant:
        logger.warning(
            f"Cannot retry log {log_id} - no active merchant for {log_entry.salla_merchant_id}"
        )
        return {"status": "error", "reason": "no_active_merchant"}
    
    # Reset the log entry status
    log_entry.status = "pending"
    log_entry.error_message = None
    log_entry.completed_at = None
    db.session.commit()
    
    logger.info(
        f"[RETRY] Retrying webhook {log_entry.request_id} "
        f"(event: {log_entry.event_type}, merchant: {log_entry.salla_merchant_id})"
    )
    
    # Load stored headers
    try:
        headers_dict = json.loads(log_entry.headers) if log_entry.headers else {}
    except json.JSONDecodeError:
        headers_dict = {}
    
    # Get target URL and settings
    target_url = merchant.odoo_url.rstrip("/")
    timeout = merchant.timeout_seconds or Config.WEBHOOK_TIMEOUT
    
    # Add custom headers if configured
    if merchant.custom_headers:
        try:
            custom = json.loads(merchant.custom_headers)
            headers_dict.update(custom)
        except json.JSONDecodeError:
            pass
    
    # Add tracking header
    headers_dict["X-Request-ID"] = log_entry.request_id
    headers_dict["X-Retry-Count"] = str(log_entry.retry_count or 0)
    
    try:
        # Forward the webhook
        session = get_http_session()
        payload_bytes = log_entry.payload.encode('utf-8')
        
        response = session.post(
            target_url,
            data=payload_bytes,
            headers=headers_dict,
            timeout=timeout,
        )
        
        response.raise_for_status()
        
        # Success!
        merchant.record_success()
        log_entry.mark_forwarded(response.status_code, response.text[:500])
        log_entry.retry_count = (log_entry.retry_count or 0) + 1
        db.session.commit()
        
        logger.info(
            f"[RETRY SUCCESS] {log_entry.request_id} → {target_url} "
            f"(status: {response.status_code})"
        )
        
        return {
            "status": "forwarded",
            "request_id": log_entry.request_id,
            "odoo_status": response.status_code,
        }
        
    except requests.exceptions.RequestException as exc:
        error_msg = str(exc)
        logger.error(
            f"[RETRY FAILED] {log_entry.request_id} - {error_msg}"
        )
        log_entry.mark_failed(f"Retry failed: {error_msg}")
        log_entry.retry_count = (log_entry.retry_count or 0) + 1
        merchant.record_failure()
        db.session.commit()
        
        return {
            "status": "failed",
            "request_id": log_entry.request_id,
            "error": error_msg,
        }
    
    except Exception as exc:
        logger.exception(f"[RETRY ERROR] Unexpected error for {log_entry.request_id}")
        log_entry.mark_failed(f"Unexpected error: {str(exc)}")
        db.session.commit()
        return {
            "status": "error",
            "request_id": log_entry.request_id,
            "message": str(exc),
        }


@celery_app.task(bind=True)
def cleanup_old_logs(self, days: int = 30) -> Dict[str, Any]:
    """
    Periodic task to clean up old webhook logs.
    
    Schedule via Celery Beat:
        celery -A celery_app:celery_app beat -l info
    """
    from models import db, WebhookLog
    from datetime import timedelta
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    deleted = WebhookLog.query.filter(
        WebhookLog.received_at < cutoff,
        WebhookLog.status.in_(["forwarded", "ignored"])
    ).delete(synchronize_session=False)
    
    db.session.commit()
    
    logger.info(f"Cleaned up {deleted} webhook logs older than {days} days")
    
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


# ====================== CELERY BEAT SCHEDULE ======================

celery_app.conf.beat_schedule = {
    "cleanup-old-logs": {
        "task": "tasks.cleanup_old_logs",
        "schedule": 86400,  # Daily
        "args": (30,),  # Keep 30 days
    },
}