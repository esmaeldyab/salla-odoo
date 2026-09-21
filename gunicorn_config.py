import multiprocessing

bind = "0.0.0.0:5000"
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
worker_connections = 1000
keepalive = 5
timeout = 120
max_requests = 1000
max_requests_jitter = 50

# Logging
accesslog = "/var/www/salla-webhook/logs/gunicorn_access.log"
errorlog = "/var/www/salla-webhook/logs/gunicorn_error.log"
loglevel = "info"

# Process naming
proc_name = "salla_webhook_router"

# Server mechanics
daemon = False
pidfile = "/var/www/salla-webhook/gunicorn.pid"
