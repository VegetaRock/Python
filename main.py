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
    # Acrobat-like fit-page rendering for A0/A1 engineering drawings.
    screen_matched_rendering=True,
    graphics_min_line_width_px=1.0,
    minimum_render_dpi=24,
    maximum_render_dpi=600,
    render_quality=1.0,
    cache_limit_mb=512,
    # A4 text = 15 pt; larger sheets auto-scale.
    auto_text_size_by_paper=True,
    base_text_size_a4_pt=15.0,
)
editor.showMaximized()

sys.exit(app.exec())