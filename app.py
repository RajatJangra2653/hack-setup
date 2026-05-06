"""Production Flask app for Azure App Service deployment.

Serves the frontend + API endpoints for OneDrive provisioning and file uploads.
Designed for Azure App Service (Linux, B1+ plan) with gunicorn.

Start locally:  python app.py
Production:     gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 2 app:app
"""
from __future__ import annotations

import os
import sys

from flask import Flask, request, abort

# -- Load .env (best-effort) --
try:
    from dotenv import load_dotenv  # type: ignore
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.isfile(_env_path):
        load_dotenv(_env_path, override=False)
        print(f"[ENV] Loaded {_env_path}")
except ImportError:
    pass

# -- Add src to path --
_app_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_app_dir, "src"))
sys.path.insert(0, _app_dir)

from routes import register_blueprints

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

register_blueprints(app)

# -- Domain validation middleware (Easy Auth sets X-MS-CLIENT-PRINCIPAL-NAME) --
ALLOWED_DOMAINS = {"spektrasystems.com", "copilot4cloudlabs.onmicrosoft.com"}

@app.before_request
def _validate_domain():
    email = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "")
    if not email:
        return  # local dev or Easy Auth not enabled
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if domain not in ALLOWED_DOMAINS:
        abort(403, description="Access denied: your organisation is not permitted.")


if __name__ == "__main__":
    print("Starting local dev server at http://localhost:4280")
    app.run(host="0.0.0.0", port=4280, debug=True)
