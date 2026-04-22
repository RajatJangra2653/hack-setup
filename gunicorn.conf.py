# Gunicorn config for Azure App Service
# Long timeout for bulk upload operations (10 min)
bind = "0.0.0.0:8000"
workers = 2
threads = 4
timeout = 600
max_requests = 1000
max_requests_jitter = 50
accesslog = "-"
errorlog = "-"
loglevel = "info"
