"""PDF generation for large user lists (> 100 users)."""

from __future__ import annotations

from io import BytesIO
from typing import Any

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib import colors


def generate_user_list_pdf(users: list[dict[str, Any]]) -> BytesIO:
    """Return a BytesIO PDF containing the user list."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=1 * inch,
        bottomMargin=1 * inch,
    )
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph("Registered Bot Users", styles["Title"]))
    elements.append(Spacer(1, 0.2 * inch))
    elements.append(Paragraph(f"Total users: {len(users)}", styles["Normal"]))
    elements.append(Spacer(1, 0.2 * inch))

    header = ["#", "Display Name", "Username", "Joined"]
    rows = [header]
    for i, u in enumerate(users, start=1):
        joined = u.get("joined", "")
        if hasattr(joined, "strftime"):
            joined = joined.strftime("%Y-%m-%d")
        rows.append([
            str(i),
            u.get("display_name") or "—",
            ("@" + u["username"]) if u.get("username") else "—",
            str(joined),
        ])

    col_widths = [0.5 * inch, 2.5 * inch, 2 * inch, 1.5 * inch]
    table = Table(rows, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer
