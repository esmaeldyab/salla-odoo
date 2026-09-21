bind = "0.0.0.0:5000"

# بداية متوازنة لسيرفر 6 vCPU و12 GB RAM
workers = 6
worker_class = "sync"

keepalive = 5
timeout = 120
graceful_timeout = 30

max_requests = 1000
max_requests_jitter = 50

accesslog = "-"
errorlog = "-"
loglevel = "info"

proc_name = "salla_webhook_router"
daemon = False
pidfile = None
