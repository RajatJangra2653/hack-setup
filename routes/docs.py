"""Document generation routes."""
from __future__ import annotations

from flask import Blueprint, Response, request, jsonify

from onedrive_provisioner.docgen import DocGenerator

from ._state import get_state_manager, generated_docs, docs_lock

bp = Blueprint("docs", __name__)


@bp.route("/api/generate-doc", methods=["POST"])
def generate_doc():
    data = request.get_json(silent=True) or {}
    prefix = (data.get("hackPrefix") or data.get("prefix") or "").strip()
    if not prefix:
        return jsonify({"error": "hackPrefix is required"}), 400

    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503

    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404

    try:
        gen = DocGenerator()
        doc_bytes = gen.generate(state)
        filename = gen.get_filename(state)
        return Response(
            doc_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/generated-docs/<doc_id>", methods=["GET"])
def download_generated_doc(doc_id):
    with docs_lock:
        entry = generated_docs.get(doc_id)
    if not entry:
        return jsonify({"error": "Document not found or expired"}), 404
    return Response(
        entry["data"],
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{entry["filename"]}"'},
    )
