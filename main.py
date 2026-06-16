"""
Example entry point for PdfCorrectionEditor.

Replace input_path/output_path with your own values. The approve callback is
triggered only after:
    1. correction PDF is saved to output_path, and
    2. watermark is applied to the existing/source PDF, and
    3. that source PDF is reopened in the viewer.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

# HiDPI / Retina support. Must be set BEFORE creating QApplication.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

from pdf_editor_controller import PdfCorrectionEditor  # noqa: E402


def on_approve_completed(source_pdf_path: str, correction_pdf_path: str) -> None:
    """
    Put your main-window approve logic here.

    source_pdf_path:
        Existing input PDF path. The watermark is saved back to this same file.
    correction_pdf_path:
        Separate annotated/correction PDF saved in the output folder/path.
    """
    print("Approve completed")
    print("Watermarked source PDF:", source_pdf_path)
    print("Saved correction PDF:", correction_pdf_path)


editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Part1.pdf",
    output_path=r"D:\aPPROVE",
    # Good Acrobat-like fit-page rendering for engineering drawings.
    screen_matched_rendering=True,
    graphics_min_line_width_px=1.0,
    minimum_render_dpi=24,
    maximum_render_dpi=600,
    render_quality=1.0,
    cache_limit_mb=512,
    # Text size: A4 = 15 pt, larger sheets scale automatically.
    auto_text_size_by_paper=True,
    base_text_size_a4_pt=15.0,
)

# This signal is emitted after watermark + reopen completes.
editor.approve_completed.connect(on_approve_completed)

editor.showMaximized()
sys.exit(app.exec())
