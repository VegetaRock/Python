"""
Entry point for the PDF Correction Editor.

Usage:
    python main.py input.pdf output_folder_or_output.pdf

You can also edit the two paths below for quick testing.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

# HiDPI / Retina support. Must be set before QApplication is created.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

from pdf_editor_controller import PdfCorrectionEditor  # noqa: E402

# if len(sys.argv) >= 3:
#     input_pdf = sys.argv[1]
#     output_path = sys.argv[2]
# else:
#     QMessageBox.information(
#         None,
#         "Usage",
#         "Run:\npython main.py input.pdf output_folder_or_output.pdf",
#     )
#     raise SystemExit(1)

editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Part1.pdf",
    output_path=r"D:\aPPROVE",
    # Acrobat-like A0/A1 display: screen-matched rendering + MuPDF thin-line hint.
    screen_matched_rendering=True,
    graphics_min_line_width_px=1.0,
    render_quality=1.0,
    minimum_render_dpi=24,
    maximum_render_dpi=600,
    # Text size: A4 = 15 pt, larger sheets scale by paper area.
    auto_text_size_by_paper=True,
    base_text_size_a4_pt=15.0,
)
editor.showMaximized()

sys.exit(app.exec())
