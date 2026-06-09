"""
Entry point for the PDF Correction Editor.

Usage:
    python main.py
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

# ── HiDPI / Retina support ─────────────────────────────────────────────────
# Must be set BEFORE creating QApplication.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

# ── Import after QApplication exists (controller imports Qt widgets) ───────
from pdf_editor_controller import PdfCorrectionEditor  # noqa: E402

editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Part1.pdf",
    output_path=r"D:\aPPROVE",
    # Rendering: 150 dpi floor keeps large A0/A1 sheets crisp at fit-to-page.
    # The engine automatically raises DPI as you zoom in, up to the maximum.
    minimum_render_dpi=150,
    maximum_render_dpi=600,
    # render_quality=3.0 means tiles are rendered at 3× the current screen zoom,
    # producing sharp results even on large engineering drawings.
    render_quality=3.0,
    cache_limit_mb=512,
)
editor.show()

sys.exit(app.exec())