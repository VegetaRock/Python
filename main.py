"""
Entry point example for PdfCorrectionEditor.

Replace input_path/output_path with your own values, or import
PdfCorrectionEditor from your application and connect the same signals.
"""

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

from pdf_editor_controller import PdfCorrectionEditor  # noqa: E402


def on_approve_completed(source_pdf_path: str, correction_pdf_path: str) -> None:
    """Triggered after Approve saves correction, watermarks source, and reopens it."""
    print("Approve completed")
    print("Watermarked source PDF:", source_pdf_path)
    print("Saved correction PDF:", correction_pdf_path)


def on_correction_completed(correction_pdf_path: str, deleted_source_pdf_path: str) -> None:
    """Triggered after Correction saves output and deletes the source PDF."""
    print("Correction completed")
    print("Saved correction PDF:", correction_pdf_path)
    print("Deleted source PDF:", deleted_source_pdf_path or "not deleted")


editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Part1.pdf",
    output_path=r"D:\aPPROVE",
    # Acrobat-like screen-matched rendering for A0 engineering drawings.
    screen_matched_rendering=True,
    graphics_min_line_width_px=1.0,
    render_quality=1.0,
    minimum_render_dpi=24,
    maximum_render_dpi=600,
    cache_limit_mb=512,
    # Text size auto-scales from A4 = 15 pt.
    auto_text_size_by_paper=True,
    base_text_size_a4_pt=15.0,
)
editor.approve_completed.connect(on_approve_completed)
editor.correction_completed.connect(on_correction_completed)
editor.showMaximized()

sys.exit(app.exec())
