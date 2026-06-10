"""
Entry point for the PDF Correction Editor.

Usage:
    python main.py
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

# HiDPI / Retina support. Must be set before creating QApplication.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

# Import after QApplication exists because the controller imports Qt widgets.
from pdf_editor_controller import PdfCorrectionEditor  # noqa: E402

editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Part1.pdf",
    output_path=r"D:\aPPROVE",

    # Restored previous best rendering: Acrobat-like screen-matched tiles.
    screen_matched_rendering=True,
    graphics_min_line_width_px=1.0,
    minimum_render_dpi=24,
    maximum_render_dpi=600,
    render_quality=1.0,
    cache_limit_mb=512,

    # Text annotations: A4=15 pt, larger sheets scale automatically.
    auto_text_size_by_paper=True,
    base_text_size_a4_pt=15.0,
)
editor.showMaximized()

sys.exit(app.exec())
