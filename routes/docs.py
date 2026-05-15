"""Document generation routes."""
from __future__ import annotations

import io

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


# ────────────────────── PDF Credential Cards ──────────────────────

def _generate_pdf_cards(state: dict) -> bytes:
    """Generate a PDF with 4 credential cards per A4 page, each with a QR code."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas
    import qrcode

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    pw, ph = A4  # 595.28, 841.89 points

    users = state.get("users") or []
    if not users:
        c.drawString(72, ph - 72, "No users found in hack state.")
        c.save()
        return buf.getvalue()

    hack_name = state.get("hackName") or state.get("config", {}).get("hackName") or state.get("prefix", "")
    domain = state.get("domain") or state.get("config", {}).get("domain") or ""
    login_url = "https://login.microsoftonline.com/"

    # Card layout: 2 columns × 2 rows = 4 per page
    margin = 15 * mm
    gap = 5 * mm
    card_w = (pw - 2 * margin - gap) / 2
    card_h = (ph - 2 * margin - gap) / 2
    positions = [
        (margin, ph - margin - card_h),               # top-left
        (margin + card_w + gap, ph - margin - card_h), # top-right
        (margin, ph - margin - 2 * card_h - gap),      # bottom-left
        (margin + card_w + gap, ph - margin - 2 * card_h - gap),  # bottom-right
    ]

    for idx, user in enumerate(users):
        slot = idx % 4
        if slot == 0 and idx > 0:
            c.showPage()
        cx, cy = positions[slot]
        upn = user.get("userPrincipalName", "")
        pwd = user.get("password", "—")

        # Card border
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.5)
        c.roundRect(cx, cy, card_w, card_h, 6, stroke=1, fill=0)

        # Header bar
        c.setFillColorRGB(0.15, 0.25, 0.6)
        c.rect(cx, cy + card_h - 28, card_w, 28, stroke=0, fill=1)
        c.setFillColorRGB(1, 1, 1)
        c.setFont("Helvetica-Bold", 10)
        title = hack_name[:40] if hack_name else "Hackathon Credentials"
        c.drawString(cx + 8, cy + card_h - 20, title)

        # UPN
        y = cy + card_h - 50
        c.setFillColorRGB(0, 0, 0)
        c.setFont("Helvetica", 8)
        c.drawString(cx + 8, y, "Username:")
        c.setFont("Helvetica-Bold", 9)
        # Truncate long UPNs
        display_upn = upn if len(upn) <= 42 else upn[:39] + "..."
        c.drawString(cx + 8, y - 14, display_upn)

        # Password
        y -= 36
        c.setFont("Helvetica", 8)
        c.drawString(cx + 8, y, "Password:")
        c.setFont("Courier-Bold", 10)
        display_pwd = pwd if pwd and pwd != "—" else "(see admin)"
        c.drawString(cx + 8, y - 14, display_pwd[:30])

        # Login URL
        y -= 36
        c.setFont("Helvetica", 7)
        c.setFillColorRGB(0.3, 0.3, 0.3)
        c.drawString(cx + 8, y, f"Login: {login_url}")

        # QR code
        qr = qrcode.QRCode(version=1, box_size=3, border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(login_url)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_buf = io.BytesIO()
        qr_img.save(qr_buf, format="PNG")
        qr_buf.seek(0)
        from reportlab.lib.utils import ImageReader
        qr_size = 55
        c.drawImage(ImageReader(qr_buf), cx + card_w - qr_size - 10, cy + 10, qr_size, qr_size)

        # Footer
        c.setFont("Helvetica", 6)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawString(cx + 8, cy + 6, f"#{idx + 1}")

    c.save()
    return buf.getvalue()


@bp.route("/api/hack-state/<prefix>/cards.pdf", methods=["GET"])
def download_credential_cards(prefix):
    """Generate a PDF of printable credential cards (4 per page) for a hack."""
    mgr = get_state_manager()
    if not mgr:
        return jsonify({"error": "Storage not configured"}), 503
    state = mgr.get_state(prefix)
    if not state:
        return jsonify({"error": f"No state found for prefix '{prefix}'"}), 404
    try:
        pdf_bytes = _generate_pdf_cards(state)
        filename = f"{prefix}credential-cards.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
