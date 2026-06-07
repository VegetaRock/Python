# PySide6 PDF Correction Editor

This package contains a reusable `PdfCorrectionEditor` class built on your generated `Ui_MainWindow` layout.

## Install

```bash
pip install -r requirements_pdf_editor_class.txt
```

## Run directly

```bash
python pdf_editor_controller.py input.pdf output_folder_or_output.pdf
```

## Use in another app

```python
from pdf_editor_controller import PdfCorrectionEditor

editor = PdfCorrectionEditor(
    input_path=r"C:\path\to\drawing.pdf",
    output_path=r"C:\path\to\output_folder",  # folder or .pdf path
    minimum_render_dpi=144,
    maximum_render_dpi=360,
)
editor.show()
```

## Controls

- Mouse wheel: zoom in/out around the mouse pointer.
- Middle mouse button drag: pan; cursor changes to closed hand while panning.
- Circle button: drag to draw a circle.
- Arrow button: drag to draw an arrow.
- Text button: click the PDF and enter text. The spin box controls text size in PDF points.
- Ctrl+Z: undo last annotation.
- Delete: delete selected annotation.
- PageUp/PageDown: change page.
- Correction button: save the annotated PDF to the configured output path.

## Crash-safe rendering

The viewer does not rasterize the entire sheet as one 300 DPI pixmap. It renders visible PDF tiles/clips and caps memory usage, which avoids native Windows crashes like `0xC0000409` on large engineering drawings. Saved annotations are vector PDF drawings/text and the original PDF page is not rasterized.
