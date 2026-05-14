"""Register all route blueprints on the Flask app."""
from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .static import bp as static_bp
    from .upload import bp as upload_bp
    from .provision import bp as provision_bp
    from .github import bp as github_bp
    from .tenant import bp as tenant_bp
    from .hack_state import bp as hack_state_bp
    from .scheduler import bp as scheduler_bp
    from .docs import bp as docs_bp
    from .chat import bp as chat_bp
    from .lifecycle import bp as lifecycle_bp

    for blueprint in (
        static_bp,
        upload_bp,
        provision_bp,
        github_bp,
        tenant_bp,
        hack_state_bp,
        scheduler_bp,
        docs_bp,
        chat_bp,
        lifecycle_bp,
    ):
        app.register_blueprint(blueprint)
