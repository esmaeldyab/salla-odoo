"""
Salla → Odoo Webhook Router
A Flask application to receive webhooks from Salla and forward them to Odoo instances.
Features:
- Async processing via Celery
- Admin panel for merchant management
- Request tracking and logging
- Signature verification
- Configurable event blocking
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


# ====================== LOGGING SETUP ======================

def setup_logging(app: Flask) -> None:
    """Configure application logging."""
    os.makedirs(Config.LOG_DIR, exist_ok=True)
    
    # File handler
    file_handler = logging.FileHandler(f"{Config.LOG_DIR}/app.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    file_handler.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    console_handler.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # Configure Flask logger
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.setLevel(getattr(logging, Config.LOG_LEVEL))
    
    # Also configure root logger for libraries
    logging.getLogger().addHandler(file_handler)


# ====================== APPLICATION FACTORY ======================

def create_app() -> Flask:
    """
    Application factory function.
    
    Creates and configures the Flask application with all extensions.
    """
    app = Flask(__name__)
    
    # Load configuration
    config = get_config()
    app.config.from_object(config)
    
    # Handle proxy headers (for running behind nginx/load balancer)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    
    # Setup logging
    setup_logging(app)
    
    # Initialize extensions
    db.init_app(app)
    init_login_manager(app)
    init_admin(app)
    
    # Register routes
    register_routes(app)
    
    # Register CLI commands
    register_cli_commands(app)
    
    return app


# ====================== LOGIN MANAGER ======================

def init_login_manager(app: Flask) -> None:
    """Initialize Flask-Login."""
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id: str) -> User:
        return db.session.get(User, int(user_id))


# ====================== ADMIN PANEL ======================

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
        "merchant_id", "name", "odoo_url", "active",
        "webhook_count", "last_webhook_at", "failure_count"
    ]
    column_searchable_list = ["merchant_id", "name", "odoo_url"]
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
        "created_at", "updated_at"
    ]
    
    can_export = True
    can_view_details = True
    page_size = 25


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
    can_delete = True  # Allow cleanup
    can_export = True
    can_view_details = True
    page_size = 50
    
    # Add custom actions
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
        """Hash password if provided."""
        if form.password.data:
            model.set_password(form.password.data)


class DashboardView(SecureAdminIndexView):
    """Custom dashboard view with metrics."""
    
    @expose('/')
    def index(self):
        """Render dashboard with statistics."""
        # Get statistics
        merchants_total = Merchant.query.count()
        merchants_active = Merchant.query.filter_by(active=True).count()
        
        webhooks_forwarded = WebhookLog.query.filter_by(status="forwarded").count()
        webhooks_pending = WebhookLog.query.filter_by(status="pending").count()
        webhooks_failed = WebhookLog.query.filter_by(status="failed").count()
        total_webhooks = WebhookLog.query.count()
        
        # Get active merchants (with recent activity)
        active_merchants = Merchant.query.filter_by(active=True)\
            .order_by(Merchant.last_webhook_at.desc())\
            .limit(10)\
            .all()
        
        # Get recent logs
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


# ====================== ROUTES ======================

def register_routes(app: Flask) -> None:
    """Register all application routes."""
    
    from tasks import forward_webhook, verify_signature, compute_payload_hash
    
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
        
        # Prepare headers for forwarding
        headers_to_forward = {
            k: v for k, v in request.headers.items()
            if k.lower() not in (
                "host", "content-length", "connection",
                "accept-encoding", "transfer-encoding"
            )
        }
        
        # Update merchant statistics (quick update)
        merchant = Merchant.query.filter_by(
            merchant_id=merchant_id, active=True
        ).first()
        if merchant:
            merchant.increment_webhook_count()
            db.session.commit()
        
        # Queue for async processing
        # Convert bytes to string for JSON serialization
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
    
    @app.route("/health")
    def health():
        """
        Health check endpoint.
        
        Returns basic health status. For production, consider adding
        checks for database and Celery connectivity.
        """
        health_status = {
            "status": "healthy",
            "service": "salla-odoo-router",
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        # Optional: Check database connectivity
        try:
            db.session.execute(db.text("SELECT 1"))
            health_status["database"] = "connected"
        except Exception as e:
            health_status["status"] = "degraded"
            health_status["database"] = f"error: {str(e)}"
        
        status_code = 200 if health_status["status"] == "healthy" else 503
        return jsonify(health_status), status_code
    
    @app.route("/health/ready")
    def readiness():
        """Kubernetes-style readiness probe."""
        try:
            # Check database
            db.session.execute(db.text("SELECT 1"))
            return jsonify({"ready": True}), 200
        except Exception:
            return jsonify({"ready": False}), 503
    
    @app.route("/health/live")
    def liveness():
        """Kubernetes-style liveness probe."""
        return jsonify({"alive": True}), 200
    
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
        print("✓ Database tables created")
        
        # Create default admin if not exists
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin")
            admin.set_password(Config.ADMIN_PASSWORD)
            db.session.add(admin)
            print("✓ Default admin user created")
        
        # Add sample merchant if empty
        if Merchant.query.count() == 0:
            sample = Merchant(
                merchant_id="sample_merchant_001",
                name="Sample Store",
                odoo_url="https://your-odoo.com/salla/webhook",
                active=False,  # Disabled by default
            )
            db.session.add(sample)
            print("✓ Sample merchant created (disabled)")
        
        # Add default blocked events
        default_blocked = [
            ("order.status.updated", "High volume, usually not needed"),
        ]
        for event_type, reason in default_blocked:
            if not BlockedEvent.query.filter_by(event_type=event_type).first():
                blocked = BlockedEvent(event_type=event_type, reason=reason)
                db.session.add(blocked)
        
        db.session.commit()
        print("✓ Database initialization complete")
    
    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_user(username: str, password: str):
        """Create a new admin user."""
        if User.query.filter_by(username=username).first():
            print(f"✗ User '{username}' already exists")
            return
        
        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"✓ User '{username}' created")
    
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
        print(f"✓ Deleted {deleted} logs older than {days} days")


# ====================== APPLICATION INSTANCE ======================

app = create_app()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀 Salla → Odoo Webhook Router")
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