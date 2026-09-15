"""
Salla Odoo Webhook Router
A Flask application to receive webhooks from Salla and forward them to Odoo instances.
"""
import os
import logging
from uuid import uuid4
from datetime import datetime

from flask import Flask, request, jsonify, abort, redirect, url_for, flash, has_app_context
from flask_login import (
    LoginManager, login_user, login_required, logout_user, current_user
)
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.contrib.sqla import ModelView
from flask_admin.actions import action
from flask_admin.contrib.sqla import filters as sqla_filters
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
import click

from sqlalchemy import inspect, text

from config import get_config, Config
from models import db, User, Merchant, WebhookLog, BlockedEvent
from email_utils import send_welcome_email, send_notification_email

def setup_logging(app: Flask) -> None:
    """Configure application logging."""
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    
    file_handler = logging.FileHandler(f"{Config.LOG_DIR}/app.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    console_handler.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    logging.getLogger().addHandler(file_handler)



def create_app() -> Flask:
    app = Flask(__name__)
    
    config = get_config()
    app.config.from_object(config)
    
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    
    setup_logging(app)
    
    db.init_app(app)
    init_login_manager(app)
    init_admin(app)
    
    register_routes(app)
    
    register_cli_commands(app)
    
    return app



def init_login_manager(app: Flask) -> None:
    """Initialize Flask-Login."""
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str) -> User:
        return db.session.get(User, int(user_id))



class SecureAdminIndexView(AdminIndexView):
    """Secured admin index view."""
    
    def is_accessible(self) -> bool:
        return current_user.is_authenticated and current_user.is_active

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for("login"))


class SecureModelView(ModelView):
    """Base secure model view for all admin views."""
    
    def is_accessible(self) -> bool:
        return current_user.is_authenticated and current_user.is_active
    
    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for("login"))


def _copy_button(value: str) -> Markup:
    return Markup(
        '<button type="button" class="btn btn-sm btn-outline-secondary copy-btn" '
        'data-copy="{}" title="Copy"><i class="fas fa-copy"></i></button>'
    ).format(value)


def _secret_list_formatter(view, context, model, name):
    value = getattr(model, name)
    if not value:
        return "-"
    return Markup('<span class="text-monospace">{}&hellip;</span> {}').format(
        value[:10], _copy_button(value)
    )


def _secret_detail_formatter(view, context, model, name):
    value = getattr(model, name)
    if not value:
        return "-"
    return Markup(
        '<div class="d-flex align-items-start gap-2">'
        '<code class="text-break flex-grow-1">{}</code>{}</div>'
    ).format(value, _copy_button(value))


class MerchantView(SecureModelView):
    """Admin view for Merchant model."""
    
    column_list = [
        "merchant_id", "name", "email", "odoo_url", "odoo_database", "api_key",
        "access_token", "refresh_token", "active",
        "webhook_count", "last_webhook_at", "failure_count"
    ]
    column_searchable_list = ["merchant_id", "name", "email", "odoo_url", "odoo_database"]
    column_filters = ["active", "created_at", "last_webhook_at"]
    column_editable_list = ["active"]
    column_sortable_list = [
        "merchant_id", "name", "active", "webhook_count",
        "last_webhook_at", "failure_count", "created_at"
    ]
    
    column_labels = {
        "merchant_id": "Merchant ID",
        "odoo_url": "Odoo Webhook URL",
        "odoo_database": "Odoo Database",
        "api_key": "Odoo API Key",
        "access_token": "Access Token",
        "refresh_token": "Refresh Token",
        "webhook_count": "Webhooks",
        "last_webhook_at": "Last Webhook",
        "failure_count": "Failures",
        "retry_backof": "Retry every (min)",
    }
    
    column_formatters = {
        "last_webhook_at": lambda v, c, m, p: (
            m.last_webhook_at.strftime("%Y-%m-%d %H:%M") 
            if m.last_webhook_at else "-"
        ),
        "api_key": _secret_list_formatter,
        "access_token": _secret_list_formatter,
        "refresh_token": _secret_list_formatter,
    }

    column_formatters_detail = {
        "api_key": _secret_detail_formatter,
        "access_token": _secret_detail_formatter,
        "refresh_token": _secret_detail_formatter,
    }

    column_descriptions = {
        "odoo_database": (
            "Leave empty when the Odoo server hosts a single database. "
            "Otherwise enter the exact database name webhooks must be delivered to."
        ),
    }
    
    form_excluded_columns = [
        "logs", "webhook_count", "last_webhook_at",
        "last_success_at", "last_failure_at", "failure_count",
        "created_at", "updated_at",
        "access_token", "refresh_token", "api_key",
    ]

    can_export = True
    can_view_details = True
    page_size = 25

    @action(
        "regenerate_api_key",
        "Regenerate API key",
        "This invalidates the merchant's current key and their Odoo will stop "
        "syncing until the new key is entered. Continue?",
    )
    def action_regenerate_api_key(self, ids):
        """Issue a fresh API key for the selected merchants."""
        count = 0
        for merchant in Merchant.query.filter(Merchant.id.in_(ids)).all():
            merchant.generate_api_key()
            count += 1
        db.session.commit()
        flash(f"Regenerated the API key for {count} merchant(s).", "success")

    def on_model_change(self, form, model, is_created):
        """Send welcome email when a new merchant is created."""
        if is_created and model.email:
            try:
                email_sent = send_welcome_email(
                    merchant_name=model.name or model.merchant_id,
                    merchant_email=model.email,
                    merchant_id=model.merchant_id,
                    smtp_host=getattr(Config, 'SMTP_HOST', 'localhost'),
                    smtp_port=getattr(Config, 'SMTP_PORT', 587),
                    smtp_user=getattr(Config, 'SMTP_USER', ''),
                    smtp_password=getattr(Config, 'SMTP_PASSWORD', ''),
                    from_email=getattr(Config, 'SMTP_FROM_EMAIL', 'noreply@fsolutions.sa'),
                    from_name=getattr(Config, 'SMTP_FROM_NAME', 'FSolutions - Salla Integration')
                )
                
                if email_sent:
                    flash(f'Welcome email sent to {model.email}', 'success')
                else:
                    flash(f'Merchant created but welcome email could not be sent to {model.email}', 'warning')
            except Exception as e:
                flash(f'Merchant created but email sending failed: {str(e)}', 'warning')


class ProductIdFilter(sqla_filters.BaseSQLAFilter):
    def apply(self, query, value, alias=None):
        return query.filter(WebhookLog.salla_product_ids.like(f"%,{value.strip()},%"))

    def operation(self):
        return "contains"


def _event_type_options():
    if not has_app_context():
        return []
    rows = db.session.query(WebhookLog.event_type).distinct().order_by(WebhookLog.event_type)
    return [(event, event) for (event,) in rows if event]


def _merchant_options():
    if not has_app_context():
        return []
    return [
        (m.merchant_id, f"{m.name or m.merchant_id} ({m.merchant_id})")
        for m in Merchant.query.order_by(Merchant.name).all()
    ]


LOG_STATUS_OPTIONS = [
    ("pending", "pending"),
    ("forwarded", "forwarded"),
    ("failed", "failed"),
    ("ignored", "ignored"),
]


class WebhookLogView(SecureModelView):
    """Admin view for WebhookLog model (read-only with retry capability)."""

    column_list = [
        "received_at", "merchant.name", "salla_merchant_id", "event_type",
        "salla_order_id", "order_reference", "status", "odoo_status_code",
        "retry_count", "error_message", "request_id",
    ]
    column_searchable_list = [
        "request_id", "event_type", "salla_merchant_id", "merchant.name",
        "salla_order_id", "order_reference", "salla_customer_id",
        "salla_product_ids", "error_message",
    ]
    column_filters = [
        sqla_filters.FilterEqual(WebhookLog.salla_merchant_id, "Merchant", options=_merchant_options),
        sqla_filters.FilterLike(WebhookLog.salla_merchant_id, "Merchant ID"),
        "merchant.name",
        sqla_filters.FilterEqual(WebhookLog.event_type, "Event", options=_event_type_options),
        sqla_filters.FilterNotEqual(WebhookLog.event_type, "Event", options=_event_type_options),
        sqla_filters.FilterLike(WebhookLog.event_type, "Event"),
        sqla_filters.FilterEqual(WebhookLog.status, "Status", options=LOG_STATUS_OPTIONS),
        sqla_filters.FilterNotEqual(WebhookLog.status, "Status", options=LOG_STATUS_OPTIONS),
        sqla_filters.FilterEqual(WebhookLog.salla_order_id, "Order ID"),
        sqla_filters.FilterEqual(WebhookLog.order_reference, "Order Reference"),
        ProductIdFilter(WebhookLog.salla_product_ids, "Product ID"),
        sqla_filters.FilterEqual(WebhookLog.salla_customer_id, "Customer ID"),
        sqla_filters.FilterLike(WebhookLog.request_id, "Request ID"),
        sqla_filters.DateTimeBetweenFilter(WebhookLog.received_at, "Received"),
        sqla_filters.DateTimeGreaterFilter(WebhookLog.received_at, "Received"),
        sqla_filters.DateTimeSmallerFilter(WebhookLog.received_at, "Received"),
        sqla_filters.DateTimeBetweenFilter(WebhookLog.forwarded_at, "Forwarded"),
        sqla_filters.IntEqualFilter(WebhookLog.odoo_status_code, "Odoo HTTP Status"),
        sqla_filters.FilterEmpty(WebhookLog.odoo_status_code, "Odoo HTTP Status"),
        sqla_filters.IntEqualFilter(WebhookLog.retry_count, "Retries"),
        sqla_filters.IntGreaterFilter(WebhookLog.retry_count, "Retries"),
        sqla_filters.FilterLike(WebhookLog.error_message, "Error Message"),
        sqla_filters.FilterEmpty(WebhookLog.error_message, "Error Message"),
        sqla_filters.FilterLike(WebhookLog.odoo_response, "Odoo Response"),
        sqla_filters.FilterLike(WebhookLog.payload, "Payload"),
    ]
    column_sortable_list = [
        "received_at", "status", "event_type", "retry_count", "odoo_status_code",
        "salla_merchant_id", "salla_order_id", "order_reference",
        ("merchant.name", "merchant.name"),
    ]
    column_default_sort = ("received_at", True)  # Newest first

    column_labels = {
        "merchant.name": "Merchant",
        "salla_merchant_id": "Merchant ID",
        "event_type": "Event",
        "salla_order_id": "Order ID",
        "order_reference": "Order Ref",
        "salla_customer_id": "Customer ID",
        "salla_product_ids": "Product IDs",
        "odoo_status_code": "Odoo HTTP",
        "retry_count": "Retries",
        "error_message": "Error",
    }

    column_formatters = {
        "received_at": lambda v, c, m, p: (
            m.received_at.strftime("%Y-%m-%d %H:%M:%S") 
            if m.received_at else "-"
        ),
        "request_id": lambda v, c, m, p: m.request_id[:8] + "..." if m.request_id else "-",
        "error_message": lambda v, c, m, p: (
            (m.error_message[:80] + "...") if m.error_message and len(m.error_message) > 80
            else (m.error_message or "")
        ),
    }

    column_formatters_detail = {
        "salla_product_ids": lambda v, c, m, p: (m.salla_product_ids or "").strip(",").replace(",", ", "),
    }

    can_create = False
    can_edit = False
    can_delete = True
    can_export = True
    can_view_details = True
    page_size = 50

    @expose('/')
    def index_view(self):
        self._refresh_filters_cache()
        return super().index_view()

    @action(
        'retry_selected',
        'Retry Selected',
        'Re-send the selected webhooks to Odoo, whatever their status? '
        'Webhooks Odoo already processed will be processed again.',
    )
    def action_retry_selected(self, ids):
        """Re-send the selected webhooks, whatever their status."""
        from tasks import retry_webhook_by_id

        logs = WebhookLog.query.filter(WebhookLog.id.in_(ids)).all()
        queued = 0
        skipped = 0
        for log_entry in logs:
            if log_entry.payload:
                retry_webhook_by_id.delay(log_entry.id)
                queued += 1
            else:
                skipped += 1

        if queued:
            flash(f'Queued {queued} webhook(s) for retry.', 'success')
        if skipped:
            flash(f'Skipped {skipped} webhook(s) with no stored payload.', 'warning')

    @action('retry_all_failed', 'Retry All Failed', 'Are you sure you want to retry ALL failed webhooks?')
    def action_retry_all_failed(self, ids):
        """Retry all failed webhooks (ignores selection)."""
        from tasks import retry_webhook_by_id
        
        failed_logs = WebhookLog.query.filter_by(status='failed').all()
        
        for log_entry in failed_logs:
            retry_webhook_by_id.delay(log_entry.id)
        
        flash(f'Queued {len(failed_logs)} failed webhook(s) for retry.', 'success')


class BlockedEventView(SecureModelView):
    """Admin view for BlockedEvent model."""
    
    column_list = ["event_type", "reason", "created_at", "created_by"]
    column_searchable_list = ["event_type", "reason"]
    form_excluded_columns = ["created_at", "created_by"]
    
    def on_model_change(self, form, model, is_created):
        if is_created:
            model.created_by = current_user.username


class UserView(SecureModelView):
    """Admin view for User model."""
    
    column_list = ["id", "username", "is_active", "last_login", "created_at"]
    column_exclude_list = ["password_hash"]
    form_excluded_columns = ["password_hash", "last_login", "created_at"]
    
    # Add password field in form
    from wtforms import PasswordField
    form_extra_fields = {
        "password": PasswordField("New Password")
    }
    
    def on_model_change(self, form, model, is_created):
        if form.password.data:
            model.set_password(form.password.data)


class DashboardView(SecureAdminIndexView):
    """Custom dashboard view with metrics."""
    
    @expose('/')
    def index(self):
        """Render dashboard with statistics."""
        merchants_total = Merchant.query.count()
        merchants_active = Merchant.query.filter_by(active=True).count()
        
        webhooks_forwarded = WebhookLog.query.filter_by(status="forwarded").count()
        webhooks_pending = WebhookLog.query.filter_by(status="pending").count()
        webhooks_failed = WebhookLog.query.filter_by(status="failed").count()
        total_webhooks = WebhookLog.query.count()
        
        active_merchants = Merchant.query.filter_by(active=True)\
            .order_by(Merchant.last_webhook_at.desc())\
            .limit(10)\
            .all()
        
        recent_logs = WebhookLog.query\
            .order_by(WebhookLog.received_at.desc())\
            .limit(10)\
            .all()
        
        return self.render('dashboard.html',
                          merchants_total=merchants_total,
                          merchants_active=merchants_active,
                          webhooks_forwarded=webhooks_forwarded,
                          webhooks_pending=webhooks_pending,
                          webhooks_failed=webhooks_failed,
                          total_webhooks=total_webhooks,
                          active_merchants=active_merchants,
                          recent_logs=recent_logs)


def init_admin(app: Flask) -> None:
    """Initialize Flask-Admin."""
    admin = Admin(
        app,
        name="Salla Webhook Integration Platform",
        template_mode="bootstrap4",
        index_view=DashboardView(name="Dashboard", endpoint="dashboard"),
        base_template='sba_master.html'
    )
    
    admin.add_view(MerchantView(
        Merchant, db.session, name="Merchants", endpoint="merchants"
    ))
    admin.add_view(WebhookLogView(
        WebhookLog, db.session, name="Webhook Logs", endpoint="logs"
    ))
    admin.add_view(BlockedEventView(
        BlockedEvent, db.session, name="Blocked Events", endpoint="blocked"
    ))
    admin.add_view(UserView(
        User, db.session, name="Admin Users", endpoint="users"
    ))


def handle_app_store_authorize(app, request_id: str, merchant_id: str, payload: dict) -> None:
    """
    Handle app.store.authorize — create the merchant record (inactive) and store
    the OAuth tokens.  This fires before app.settings.updated, so odoo_url is
    left empty until settings arrive.
    """
    try:
        data = payload.get("data", {})

        access_token  = data.get("access_token", "")
        refresh_token = data.get("refresh_token", "")

        existing = Merchant.query.filter_by(merchant_id=merchant_id).first()

        if existing:
            existing.update_tokens(access_token, refresh_token)
            existing.updated_at = datetime.utcnow()
            db.session.commit()
            app.logger.info(
                f"[{request_id}] Updated OAuth tokens for existing merchant {merchant_id}"
            )
            return

        new_merchant = Merchant(
            merchant_id=merchant_id,
            name=f"Merchant {merchant_id}",
            odoo_url=None,
            active=False,
        )
        new_merchant.update_tokens(access_token, refresh_token)
        new_merchant.generate_api_key()
        db.session.add(new_merchant)
        db.session.commit()

        app.logger.info(
            f"[{request_id}] Created merchant stub for {merchant_id} "
            f"(tokens stored, awaiting app.settings.updated)"
        )

    except Exception as e:
        app.logger.error(
            f"[{request_id}] Error handling app.store.authorize: {str(e)}"
        )


def handle_app_settings_updated(app, request_id: str, merchant_id: str, payload: dict) -> None:
    """
    Handle app.settings.updated — fill in the Odoo URL, name and email on the
    merchant stub that was created by app.store.authorize.  If no stub exists
    yet (edge case / direct call) the merchant is created here instead.
    """
    try:
        data = payload.get("data", {})
        settings = data.get("settings", {})
        
        email = settings.get("email", "")
        company_name = settings.get("company", "")
        odoo_url = settings.get("url", "")
        phone_number = settings.get("phone_number", "")
        odoo_database = (settings.get("database") or settings.get("db") or "").strip()

        if odoo_url and not odoo_url.endswith("/orders") and not odoo_url.endswith("/salla/webhook/orders"):
            odoo_url = odoo_url.rstrip("/") + "/salla/webhook/orders"

        existing_merchant = Merchant.query.filter_by(merchant_id=merchant_id).first()

        if existing_merchant:
            is_new = existing_merchant.odoo_url is None

            existing_merchant.email = email or existing_merchant.email
            existing_merchant.name = company_name or existing_merchant.name or merchant_id
            existing_merchant.odoo_url = odoo_url
            if odoo_database:
                existing_merchant.odoo_database = odoo_database
            existing_merchant.updated_at = datetime.utcnow()
            db.session.commit()

            app.logger.info(
                f"[{request_id}] Updated merchant {merchant_id}: "
                f"{existing_merchant.name} ({email})"
            )

            if is_new and email:
                _send_merchant_emails(app, request_id, existing_merchant, email, odoo_url, phone_number)
            return

        # Fallback: merchant stub missing (authorize event was missed)
        app.logger.warning(
            f"[{request_id}] No merchant stub found for {merchant_id} — "
            f"creating from app.settings.updated (app.store.authorize may have been missed)"
        )
        new_merchant = Merchant(
            merchant_id=merchant_id,
            name=company_name or f"Merchant {merchant_id}",
            email=email,
            odoo_url=odoo_url,
            odoo_database=odoo_database or None,
            active=False,
        )
        new_merchant.generate_api_key()
        db.session.add(new_merchant)
        db.session.commit()

        app.logger.info(
            f"[{request_id}] Created merchant {merchant_id}: "
            f"{new_merchant.name} ({email}) — NOT ACTIVATED"
        )

        if email:
            _send_merchant_emails(app, request_id, new_merchant, email, odoo_url, phone_number)

    except Exception as e:
        app.logger.error(
            f"[{request_id}] Error handling app.settings.updated: {str(e)}"
        )


def _send_merchant_emails(app, request_id: str, merchant, email: str, odoo_url: str, phone_number: str) -> None:
    """Send welcome + support notification emails after a merchant is fully configured."""
    try:
        sent = send_welcome_email(
            merchant_name=merchant.name,
            merchant_email=email,
            merchant_id=merchant.merchant_id,
            smtp_host=Config.SMTP_HOST,
            smtp_port=Config.SMTP_PORT,
            smtp_user=Config.SMTP_USER,
            smtp_password=Config.SMTP_PASSWORD,
            from_email=Config.SMTP_FROM_EMAIL,
            from_name=Config.SMTP_FROM_NAME,
        )
        if sent:
            app.logger.info(f"[{request_id}] Welcome email sent to {email}")
        else:
            app.logger.warning(f"[{request_id}] Welcome email failed for {email}")
    except Exception as e:
        app.logger.warning(f"[{request_id}] Welcome email error: {str(e)}")

    try:
        if Config.SUPPORT_EMAIL and Config.SEND_SUPPORT_NOTIFICATIONS:
            sent = send_notification_email(
                merchant_name=merchant.name,
                merchant_email=email,
                merchant_id=merchant.merchant_id,
                odoo_url=odoo_url,
                phone_number=phone_number,
                support_email=Config.SUPPORT_EMAIL,
                smtp_host=Config.SMTP_HOST,
                smtp_port=Config.SMTP_PORT,
                smtp_user=Config.SMTP_USER,
                smtp_password=Config.SMTP_PASSWORD,
                from_email=Config.SMTP_FROM_EMAIL,
                from_name=Config.SMTP_FROM_NAME,
            )
            if sent:
                app.logger.info(f"[{request_id}] Support notification sent")
            else:
                app.logger.warning(f"[{request_id}] Support notification failed")
    except Exception as e:
        app.logger.warning(f"[{request_id}] Support notification error: {str(e)}")
        
# ====================== ROUTES ======================

def register_routes(app: Flask) -> None:
    """Register all application routes."""
    
    from tasks import forward_webhook, verify_signature
    
    @app.route("/")
    def index():
        """Root redirect to admin or login."""
        if current_user.is_authenticated:
            return redirect("/admin")
        return redirect(url_for("login"))
    
    @app.route("/login", methods=["GET", "POST"])
    def login():
        """Admin login page."""
        if current_user.is_authenticated:
            return redirect("/admin")
        
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            
            user = User.query.filter_by(username=username, is_active=True).first()
            
            if user and user.check_password(password):
                user.update_last_login()
                db.session.commit()
                login_user(user)
                app.logger.info(f"User '{username}' logged in")
                
                next_page = request.args.get("next")
                return redirect(next_page or "/admin")
            
            app.logger.warning(f"Failed login attempt for '{username}'")
            flash("Invalid username or password", "danger")
        
        return render_login_page()
    
    @app.route("/logout")
    @login_required
    def logout():
        """Logout current user."""
        app.logger.info(f"User '{current_user.username}' logged out")
        logout_user()
        return redirect(url_for("login"))
    
    @app.route("/salla/webhook", methods=["POST"])
    def salla_webhook():
        """
        Receive and process Salla webhooks.
        
        Flow:
        1. Verify signature
        2. Check if event is blocked
        3. Queue for async processing
        4. Return 200 immediately
        """
        # Generate request ID for tracking
        request_id = str(uuid4())
        
        # Get signature
        signature = request.headers.get("X-Salla-Signature", "")
        if not signature:
            app.logger.warning(f"[{request_id}] Missing signature header")
            abort(401, description="Missing signature")
        
        # Get raw payload
        raw_payload = request.get_data()
        
        # Verify signature
        if not verify_signature(raw_payload, signature, Config.SALLA_SECRET):
            app.logger.warning(
                f"[{request_id}] Invalid signature from {request.remote_addr}"
            )
            abort(401, description="Invalid signature")
        
        # Parse payload
        try:
            payload = request.get_json(force=True) or {}
        except Exception:
            payload = {}
        
        event = payload.get("event", "unknown")
        merchant_id = str(payload.get("merchant") or payload.get("store_id", ""))
        
        app.logger.info(
            f"[{request_id}] Received {event} from merchant {merchant_id}"
        )
        
        # Check blocked events (config-based)
        if event in Config.BLOCKED_EVENTS:
            app.logger.info(f"[{request_id}] Blocked event: {event}")
            return jsonify({
                "status": "ignored",
                "request_id": request_id,
                "reason": "blocked_event"
            }), 200
        
        # Check blocked events (database-based)
        if BlockedEvent.query.filter_by(event_type=event).first():
            app.logger.info(f"[{request_id}] Blocked event (db): {event}")
            return jsonify({
                "status": "ignored",
                "request_id": request_id,
                "reason": "blocked_event"
            }), 200
        
        if event == "app.store.authorize":
            handle_app_store_authorize(app, request_id, merchant_id, payload)

        if event == "app.settings.updated":
            handle_app_settings_updated(app, request_id, merchant_id, payload)

        headers_to_forward = {
            k: v for k, v in request.headers.items()
            if k.lower() not in (
                "host", "content-length", "connection",
                "accept-encoding", "transfer-encoding"
            )
        }
        
        merchant = Merchant.query.filter_by(
            merchant_id=merchant_id, active=True
        ).first()
        if merchant:
            merchant.increment_webhook_count()
            db.session.commit()
        
        # Queue for async processing
        forward_webhook.delay(
            request_id=request_id,
            merchant_id=merchant_id,
            raw_payload=raw_payload.decode('utf-8'),
            headers_dict=headers_to_forward,
        )
        
        app.logger.info(f"[{request_id}] Queued {event} for processing")
        
        return jsonify({
            "status": "queued",
            "request_id": request_id,
        }), 200
    
    @app.route("/metrics")
    def metrics():
        """Basic metrics endpoint for monitoring."""
        metrics = {
            "merchants": {
                "total": Merchant.query.count(),
                "active": Merchant.query.filter_by(active=True).count(),
            },
            "webhooks": {
                "total": WebhookLog.query.count(),
                "pending": WebhookLog.query.filter_by(status="pending").count(),
                "forwarded": WebhookLog.query.filter_by(status="forwarded").count(),
                "failed": WebhookLog.query.filter_by(status="failed").count(),
            },
        }
        return jsonify(metrics), 200

    @app.route("/api/tokens", methods=["POST"])
    def api_tokens():
        """Return the calling merchant's Salla OAuth tokens.

        The X-Api-Key header both identifies and authenticates the merchant:
        the key is looked up directly, so there is no merchant-id or URL
        parameter a caller could forge to reach another store's tokens. Any
        such fields in the request body are ignored.
        """
        request_id = str(uuid4())
        api_key = request.headers.get("X-Api-Key", "").strip()

        if not api_key:
            app.logger.warning(f"[{request_id}] /api/tokens called without a key")
            return jsonify({"error": "invalid_api_key"}), 401

        merchant = Merchant.query.filter_by(api_key=api_key, active=True).first()
        if not merchant:
            app.logger.warning(
                f"[{request_id}] /api/tokens called with an unknown or inactive key"
            )
            return jsonify({"error": "invalid_api_key"}), 401

        if not merchant.access_token:
            app.logger.info(
                f"[{request_id}] /api/tokens: merchant {merchant.merchant_id} "
                f"has no tokens yet"
            )
            return jsonify({"error": "not_authorized_yet"}), 409

        app.logger.info(
            f"[{request_id}] /api/tokens: issued tokens to merchant "
            f"{merchant.merchant_id}"
        )
        return jsonify({
            "merchant_id": merchant.merchant_id,
            "name": merchant.name,
            "access_token": merchant.access_token,
            "refresh_token": merchant.refresh_token,
        }), 200


def render_login_page() -> str:
    """Render the login page HTML."""
    from flask import render_template_string
    
    template = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Salla Webhook Integration Platform | FSolutions</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --primary-green: #0a9476;
            --secondary-green: #198b74;
            --dark-green: #006233;
            --dark-gray: #2e333a;
        }
        body {
            background: linear-gradient(135deg, var(--dark-green) 0%, var(--primary-green) 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            font-family: system-ui, -apple-system, sans-serif;
        }
        .login-card {
            background: white;
            border-radius: 1rem;
            padding: 2.5rem;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
            max-width: 420px;
            width: 100%;
        }
        .login-header {
            text-align: center;
            margin-bottom: 2rem;
        }
        .logo-container {
            margin-bottom: 1.5rem;
        }
        .logo-container img {
            max-width: 180px;
            height: auto;
        }
        .login-header h1 {
            font-size: 1.4rem;
            font-weight: 700;
            color: var(--dark-gray);
            margin-bottom: 0.5rem;
        }
        .login-header p {
            color: #888;
            font-size: 0.9rem;
            margin-bottom: 0;
        }
        .form-control {
            border-radius: 0.5rem;
            padding: 0.75rem 1rem;
            border: 2px solid #e3e6f0;
        }
        .form-control:focus {
            border-color: var(--primary-green);
            box-shadow: 0 0 0 0.2rem rgba(10, 148, 118, 0.15);
        }
        .input-group-text {
            background: #f8f9fc;
            border: 2px solid #e3e6f0;
            border-right: none;
            border-radius: 0.5rem 0 0 0.5rem;
            color: var(--primary-green);
        }
        .input-group .form-control {
            border-left: none;
            border-radius: 0 0.5rem 0.5rem 0;
        }
        .btn-login {
            background: linear-gradient(135deg, var(--primary-green), var(--secondary-green));
            border: none;
            border-radius: 0.5rem;
            padding: 0.75rem;
            font-weight: 600;
            color: white;
            width: 100%;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn-login:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 20px rgba(10, 148, 118, 0.4);
            color: white;
        }
        .alert {
            border-radius: 0.5rem;
            border: none;
        }
        .footer-text {
            text-align: center;
            margin-top: 1.5rem;
            color: #888;
            font-size: 0.85rem;
        }
        .footer-text a {
            color: var(--primary-green);
            text-decoration: none;
        }
        .footer-text a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <div class="login-header">
            <div class="logo-container">
                <img src="/static/logo.svg" alt="FSolutions" onerror="this.style.display='none'">
            </div>
            <h1>Facilitating Solutions</h1>
            <p>Salla Webhook Integration Platform</p>
        </div>
        
        {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
        {% for category, message in messages %}
        <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
            {{ message }}
            <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
        </div>
        {% endfor %}
        {% endif %}
        {% endwith %}
        
        <form method="post">
            <div class="mb-3">
                <div class="input-group">
                    <span class="input-group-text"><i class="fas fa-user"></i></span>
                    <input type="text" name="username" class="form-control" 
                           placeholder="Username" required autofocus>
                </div>
            </div>
            
            <div class="mb-4">
                <div class="input-group">
                    <span class="input-group-text"><i class="fas fa-lock"></i></span>
                    <input type="password" name="password" class="form-control" 
                           placeholder="Password" required>
                </div>
            </div>
            
            <button type="submit" class="btn btn-login">
                <i class="fas fa-sign-in-alt me-2"></i>Sign In
            </button>
        </form>
        
        <div class="footer-text">
            Powered by <a href="https://fsolutions.sa" target="_blank">FSolutions</a>
        </div>
    </div>
</body>
</html>
'''
    
    return render_template_string(template)


# ====================== CLI COMMANDS ======================

def _ensure_api_key_column() -> None:
    """Add merchants.api_key if it is missing.

    The project has no Alembic/Flask-Migrate and db.create_all() only creates
    missing tables, never new columns on an existing one. This SQL is valid on
    both SQLite and PostgreSQL.
    """
    inspector = inspect(db.engine)
    columns = {c["name"] for c in inspector.get_columns("merchants")}
    if "api_key" not in columns:
        db.session.execute(
            text("ALTER TABLE merchants ADD COLUMN api_key VARCHAR(64)")
        )
        db.session.commit()

    db.session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_merchants_api_key "
            "ON merchants (api_key)"
        )
    )
    db.session.commit()


SCHEMA_COLUMNS = {
    "merchants": [
        ("odoo_database", "VARCHAR(255)"),
    ],
    "webhook_logs": [
        ("salla_order_id", "VARCHAR(50)"),
        ("order_reference", "VARCHAR(50)"),
        ("salla_customer_id", "VARCHAR(50)"),
        ("salla_product_ids", "TEXT"),
    ],
}

SCHEMA_INDEXES = [
    ("ix_webhook_logs_salla_merchant_id", "webhook_logs", "salla_merchant_id"),
    ("ix_webhook_logs_salla_order_id", "webhook_logs", "salla_order_id"),
    ("ix_webhook_logs_order_reference", "webhook_logs", "order_reference"),
    ("ix_webhook_logs_salla_customer_id", "webhook_logs", "salla_customer_id"),
]


def _ensure_schema() -> None:
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    for table, columns in SCHEMA_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl_type in columns:
            if name not in existing:
                _run_ddl(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")
    for index, table, column in SCHEMA_INDEXES:
        if table in tables:
            _run_ddl(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({column})")


def _run_ddl(statement: str) -> None:
    try:
        db.session.execute(text(statement))
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def _backfill_log_refs(batch_size: int = 500) -> int:
    import json as _json

    _ensure_schema()
    updated = 0
    last_id = 0
    while True:
        logs = (
            WebhookLog.query
            .filter(WebhookLog.id > last_id, WebhookLog.payload.isnot(None))
            .order_by(WebhookLog.id)
            .limit(batch_size)
            .all()
        )
        if not logs:
            break
        for log_entry in logs:
            last_id = log_entry.id
            if log_entry.salla_order_id or log_entry.salla_product_ids or log_entry.salla_customer_id:
                continue
            try:
                payload = _json.loads(log_entry.payload)
            except ValueError:
                continue
            log_entry.apply_payload_refs(payload)
            updated += 1
        db.session.commit()
    return updated


def _backfill_api_keys() -> int:
    """Give every merchant without an API key a fresh one. Returns the count."""
    _ensure_api_key_column()

    merchants = Merchant.query.filter(Merchant.api_key.is_(None)).all()
    for merchant in merchants:
        merchant.generate_api_key()
    if merchants:
        db.session.commit()
    return len(merchants)


def register_cli_commands(app: Flask) -> None:
    """Register Flask CLI commands."""
    
    @app.cli.command("init-db")
    def init_db():
        """Initialize database tables and default data."""
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        
        db.create_all()
        _ensure_schema()
        print("Database tables created")
        
        # Create default admin if not exists
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin")
            admin.set_password(Config.ADMIN_PASSWORD)
            db.session.add(admin)
            print("Default admin user created")
        
        # Add sample merchant if empty
        if Merchant.query.count() == 0:
            sample = Merchant(
                merchant_id="sample_merchant_001",
                name="Sample Store",
                email="merchant@example.com",
                odoo_url="https://your-odoo.com/salla/webhook",
                active=False,  # Disabled by default
            )
            db.session.add(sample)
            print("Sample merchant created (disabled)")
        
        # Add default blocked events
        default_blocked = [
            ("order.status.updated", "High volume, usually not needed"),
        ]
        for event_type, reason in default_blocked:
            if not BlockedEvent.query.filter_by(event_type=event_type).first():
                blocked = BlockedEvent(event_type=event_type, reason=reason)
                db.session.add(blocked)
        
        db.session.commit()
        print("Database initialization complete")
    
    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(username: str, password: str):
        """Create a new admin user."""
        if User.query.filter_by(username=username).first():
            print(f"User '{username}' already exists")
            return
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"User '{username}' created")
    
    @app.cli.command("cleanup-logs")
    @click.option("--days", default=30, help="Delete logs older than N days")
    def cleanup_logs(days: int):
        """Clean up old webhook logs."""
        from datetime import timedelta
        
        cutoff = datetime.utcnow() - timedelta(days=days)
        deleted = WebhookLog.query.filter(
            WebhookLog.received_at < cutoff
        ).delete(synchronize_session=False)
        
        db.session.commit()
        print(f"Deleted {deleted} logs older than {days} days")

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Add missing columns/indexes and index order, product and customer ids of existing logs."""
        _ensure_schema()
        count = _backfill_log_refs()
        print(f"Schema up to date; indexed references on {count} existing log(s)")

    @app.cli.command("backfill-api-keys")
    def backfill_api_keys():
        """Add the api_key column if missing and generate keys for merchants."""
        count = _backfill_api_keys()
        print(f"Generated API keys for {count} merchant(s)")
        print("")
        print("Distribute these to each merchant's Odoo configuration:")
        for merchant in Merchant.query.order_by(Merchant.merchant_id).all():
            print(f"  {merchant.merchant_id}  {merchant.name}  {merchant.api_key}")

app = create_app()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Salla Odoo Webhook Router")
    print("=" * 60)
    print("\nCommands:")
    print("  flask init-db          Initialize database")
    print("  flask create-user NAME Create admin user")
    print("  flask run              Start Flask server")
    print("")
    print("  celery -A tasks:celery_app worker -l info")
    print("                         Start Celery worker")
    print("=" * 60 + "\n")
    
    app.run(host="0.0.0.0", port=5000, debug=True)