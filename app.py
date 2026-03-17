"""
Salla Odoo Webhook Router
A Flask application to receive webhooks from Salla and forward them to Odoo instances.
"""
import os
import logging
from uuid import uuid4
from datetime import datetime

from flask import Flask, request, jsonify, abort, redirect, url_for, flash
from flask_login import (
    LoginManager, login_user, login_required, logout_user, current_user
)
from flask_admin import Admin, AdminIndexView, expose
from flask_admin.contrib.sqla import ModelView
from flask_admin.actions import action
from werkzeug.middleware.proxy_fix import ProxyFix
import click

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


class MerchantView(SecureModelView):
    """Admin view for Merchant model."""
    
    column_list = [
        "merchant_id", "name", "email", "odoo_url", "active",
        "webhook_count", "last_webhook_at", "failure_count"
    ]
    column_searchable_list = ["merchant_id", "name", "email", "odoo_url"]
    column_filters = ["active", "created_at", "last_webhook_at"]
    column_editable_list = ["active"]
    column_sortable_list = [
        "merchant_id", "name", "active", "webhook_count",
        "last_webhook_at", "failure_count", "created_at"
    ]
    
    column_labels = {
        "merchant_id": "Merchant ID",
        "odoo_url": "Odoo Webhook URL",
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
    }
    
    form_excluded_columns = [
        "logs", "webhook_count", "last_webhook_at",
        "last_success_at", "last_failure_at", "failure_count",
        "created_at", "updated_at",
        "access_token", "refresh_token",
    ]
    
    can_export = True
    can_view_details = True
    page_size = 25
    
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


class WebhookLogView(SecureModelView):
    """Admin view for WebhookLog model (read-only with retry capability)."""
    
    column_list = [
        "request_id", "event_type", "salla_merchant_id",
        "status", "odoo_status_code", "retry_count", "received_at"
    ]
    column_searchable_list = ["request_id", "event_type", "salla_merchant_id"]
    column_filters = ["status", "event_type", "received_at", "retry_count"]
    column_sortable_list = [
        "received_at", "status", "event_type", "retry_count", "odoo_status_code"
    ]
    column_default_sort = ("received_at", True)  # Newest first
    
    column_formatters = {
        "received_at": lambda v, c, m, p: (
            m.received_at.strftime("%Y-%m-%d %H:%M:%S") 
            if m.received_at else "-"
        ),
        "request_id": lambda v, c, m, p: m.request_id[:8] + "..." if m.request_id else "-",
    }
    
    can_create = False
    can_edit = False
    can_delete = True
    can_export = True
    can_view_details = True
    page_size = 50
    
    def _get_retry_action_url(self, model_id):
        return url_for('retry_webhook', log_id=model_id)
    
    column_extra_row_actions = None  # Will use action instead
    
    @action('retry_selected', 'Retry Selected', 'Are you sure you want to retry the selected webhooks?')
    def action_retry_selected(self, ids):
        """Retry selected failed webhooks."""
        from tasks import retry_webhook_by_id
        
        success_count = 0
        skip_count = 0
        
        for log_id in ids:
            log_entry = WebhookLog.query.get(log_id)
            if log_entry and log_entry.status == 'failed':
                # Queue retry task
                retry_webhook_by_id.delay(log_id)
                success_count += 1
            else:
                skip_count += 1
        
        if success_count:
            flash(f'Queued {success_count} webhook(s) for retry.', 'success')
        if skip_count:
            flash(f'Skipped {skip_count} webhook(s) (not in failed status).', 'warning')
    
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

        if odoo_url and not odoo_url.endswith("/orders") and not odoo_url.endswith("/salla/webhook/orders"):
            odoo_url = odoo_url.rstrip("/") + "/salla/webhook/orders"

        existing_merchant = Merchant.query.filter_by(merchant_id=merchant_id).first()

        if existing_merchant:
            is_new = existing_merchant.odoo_url is None

            existing_merchant.email = email or existing_merchant.email
            existing_merchant.name = company_name or existing_merchant.name or merchant_id
            existing_merchant.odoo_url = odoo_url
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
            active=False,
        )
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

def register_cli_commands(app: Flask) -> None:
    """Register Flask CLI commands."""
    
    @app.cli.command("init-db")
    def init_db():
        """Initialize database tables and default data."""
        os.makedirs(Config.LOG_DIR, exist_ok=True)
        
        db.create_all()
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