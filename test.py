import sys
from PySide6.QtWidgets import QApplication
from pdf_editor_controller import PdfCorrectionEditor

app = QApplication.instance()
if app is None:
    app = QApplication(sys.argv)

editor = PdfCorrectionEditor(
    input_path=r"D:\aPPROVE\Diagram1.pdf",
    output_path=r"D:\aPPROVE",
    minimum_render_dpi=144,
    maximum_render_dpi=360,
)
editor.show()

sys.exit(app.exec())