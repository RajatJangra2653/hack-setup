"""Production Flask app for Azure App Service deployment.

Serves the frontend + API endpoints for OneDrive provisioning and file uploads.
Designed for Azure App Service (Linux, B1+ plan) with gunicorn.

Start locally:  python app.py
Production:     gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 2 app:app
"""
from __future__ import annotations

import os
import sys

from flask import Flask

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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from routes import register_blueprints

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

register_blueprints(app)


if __name__ == "__main__":
    print("Starting local dev server at http://localhost:4280")
    app.run(host="0.0.0.0", port=4280, debug=True)
