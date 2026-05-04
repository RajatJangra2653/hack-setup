"""Static file serving blueprint."""
from flask import Blueprint, send_from_directory

from ._state import FRONTEND_DIR

bp = Blueprint("static", __name__)


@bp.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@bp.route("/<path:path>")
def static_files(path):
    return send_from_directory(FRONTEND_DIR, path)
