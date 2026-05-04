"""Local dev server – thin wrapper around the Flask app.

Usage:  python dev_server.py          →  http://localhost:4280
        python dev_server.py 8080     →  http://localhost:8080

This replaces the old http.server-based dev server with Flask's built-in
development server, eliminating ~2 000 lines of duplicated route logic.
CORS headers are added automatically so the SWA CLI can proxy to this.
"""
from __future__ import annotations

import sys

from app import app


# ── CORS for local development ──
@app.after_request
def _dev_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return response


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4280
    print(f"Dev server running at http://localhost:{port}")
    print("Press Ctrl+C to stop")
    app.run(host="0.0.0.0", port=port, debug=True)
