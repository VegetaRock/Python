"""
PDF correction editor built on a Qt Designer UI.

Expected files in the same folder:
    pdf_editor_controller.py
    pdfEditorArhflp_ui.py

Install:
    pip install PySide6 PyMuPDF

Run:
    python pdf_editor_controller.py input.pdf output_folder_or_output.pdf

Use from your app:
    editor = PdfCorrectionEditor(input_path="drawing.pdf", output_path="out")
    editor.show()
"""

from __future__ import annotations

import math
import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover - older PyMuPDF imports as fitz
    import fitz  # type: ignore

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QFrame,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QWidget,
)

from pdfEditorArhflp_ui import Ui_MainWindow


# QGraphicsItem custom data roles.
ITEM_KIND_ROLE = 1001
ANNOTATION_ID_ROLE = 1002
FONT_SIZE_PT_ROLE = 1003
PEN_WIDTH_PT_ROLE = 1004

KIND_PAGE_BACKGROUND = "page_background"
KIND_PAGE_TILE = "page_tile"
KIND_CIRCLE = "circle"       # UI button name retained; drawn as oval/ellipse by drag.
KIND_RECTANGLE = "rectangle"
KIND_ARROW = "arrow"
KIND_TEXT = "text"

PDF_POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4
PAPER_MATCH_TOLERANCE_MM = 3.0

PAPER_SIZES_MM: Dict[str, Tuple[float, float]] = {
    "A0": (841, 1189),
    "A1": (594, 841),
    "A2": (420, 594),
    "A3": (297, 420),
    "A4": (210, 297),
    "A5": (148, 210),
    "Letter": (216, 279),
    "Legal": (216, 356),
    "Tabloid": (279, 432),
    "ANSI A": (216, 279),
    "ANSI B": (279, 432),
    "ANSI C": (432, 559),
    "ANSI D": (559, 864),
    "ANSI E": (864, 1118),
    "ARCH A": (229, 305),
    "ARCH B": (305, 457),
    "ARCH C": (457, 610),
    "ARCH D": (610, 914),
    "ARCH E": (914, 1219),
}


class ArrowGraphicsItem(QGraphicsPathItem):
    """Selectable and movable arrow drawn as a QPainterPath in PDF points."""

    def __init__(
        self,
        start: QPointF,
        end: QPointF,
        pen: QPen,
        head_length_pt: float,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(parent)
        self._start = QPointF(start)
        self._end = QPointF(end)
        self._head_length_pt = float(head_length_pt)
        self.setPen(pen)
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._rebuild_path()

    def set_points(self, start: QPointF, end: QPointF) -> None:
        self.prepareGeometryChange()
        self._start = QPointF(start)
        self._end = QPointF(end)
        self._rebuild_path()

    def start_scene(self) -> QPointF:
        return self.mapToScene(self._start)

    def end_scene(self) -> QPointF:
        return self.mapToScene(self._end)

    def _rebuild_path(self) -> None:
        path = QPainterPath()
        path.moveTo(self._start)
        path.lineTo(self._end)

        dx = self._end.x() - self._start.x()
        dy = self._end.y() - self._start.y()
        length = math.hypot(dx, dy)
        if length > 0.01:
            angle = math.atan2(dy, dx)
            arrow_angle = math.radians(28.0)
            head_len = min(self._head_length_pt, max(6.0, length * 0.45))

            left = QPointF(
                self._end.x() - head_len * math.cos(angle - arrow_angle),
                self._end.y() - head_len * math.sin(angle - arrow_angle),
            )
            right = QPointF(
                self._end.x() - head_len * math.cos(angle + arrow_angle),
                self._end.y() - head_len * math.sin(angle + arrow_angle),
            )
            path.moveTo(self._end)
            path.lineTo(left)
            path.moveTo(self._end)
            path.lineTo(right)

        self.setPath(path)


class PdfGraphicsView(QGraphicsView):
    """Graphics view for zoom, pan, drawing, and visible-tile updates."""

    def __init__(self, editor: "PdfCorrectionEditor", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self._drawing_start: Optional[QPointF] = None
        self._temp_item: Optional[QGraphicsItem] = None
        self._panning = False
        self._pan_start = QPoint()
        self._pan_h_value = 0
        self._pan_v_value = 0

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setBackgroundBrush(QBrush(QColor("#404040")))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # ---------- Mouse / keyboard interaction ----------

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        """Mouse wheel zooms around the mouse pointer."""
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        if delta == 0:
            event.ignore()
            return

        steps = delta / 120.0
        factor = 1.15 ** steps
        self.editor.zoom_at_mouse(factor)
        event.accept()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event.position().toPoint())
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self.editor.current_tool:
            scene_pos = self.mapToScene(event.position().toPoint())
            if not self.editor.is_point_on_page(scene_pos):
                event.accept()
                return
            scene_pos = self.editor.clamp_to_page(scene_pos)

            if self.editor.current_tool == KIND_CIRCLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_ellipse_item(QRectF(scene_pos, scene_pos))
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if self.editor.current_tool == KIND_RECTANGLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_rectangle_item(QRectF(scene_pos, scene_pos))
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if self.editor.current_tool == KIND_ARROW:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_arrow_item(scene_pos, scene_pos)
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if self.editor.current_tool == KIND_TEXT:
                self.editor.add_text_at(scene_pos)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._panning:
            self._update_pan(event.position().toPoint())
            event.accept()
            return

        if self._drawing_start is not None and self._temp_item is not None:
            scene_pos = self.editor.clamp_to_page(self.mapToScene(event.position().toPoint()))
            modifiers = QApplication.keyboardModifiers()
            force_square = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

            if self.editor.current_tool == KIND_CIRCLE and isinstance(self._temp_item, QGraphicsEllipseItem):
                # Normal drag = oval/ellipse. Shift + drag = perfect circle.
                self._temp_item.setRect(self._drag_rect(self._drawing_start, scene_pos, force_square))
                event.accept()
                return

            if self.editor.current_tool == KIND_RECTANGLE and isinstance(self._temp_item, QGraphicsRectItem):
                self._temp_item.setRect(self._drag_rect(self._drawing_start, scene_pos, force_square))
                event.accept()
                return

            if self.editor.current_tool == KIND_ARROW and isinstance(self._temp_item, ArrowGraphicsItem):
                self._temp_item.set_points(self._drawing_start, scene_pos)
                event.accept()
                return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._stop_pan()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._temp_item is not None:
            item = self._temp_item
            self._temp_item = None
            self._drawing_start = None

            if self.editor.is_too_small_annotation(item):
                self.scene().removeItem(item)
            else:
                self.editor.register_new_annotation(item)
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.matches(QKeySequence.StandardKey.Undo):
            self.editor.undo_last_draw()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.editor.delete_selected_annotations()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.editor.set_tool(None)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageDown:
            self.editor.go_to_page(self.editor.current_page_index + 1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_PageUp:
            self.editor.go_to_page(self.editor.current_page_index - 1)
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------- Pan helpers ----------

    def _start_pan(self, pos: QPoint) -> None:
        self._panning = True
        self._pan_start = QPoint(pos)
        self._pan_h_value = self.horizontalScrollBar().value()
        self._pan_v_value = self.verticalScrollBar().value()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _update_pan(self, pos: QPoint) -> None:
        delta = pos - self._pan_start
        self.horizontalScrollBar().setValue(self._pan_h_value - delta.x())
        self.verticalScrollBar().setValue(self._pan_v_value - delta.y())

    def _stop_pan(self) -> None:
        self._panning = False
        self.editor.apply_cursor_for_tool()

    @staticmethod
    def _drag_rect(start: QPointF, end: QPointF, force_square: bool) -> QRectF:
        if not force_square:
            return QRectF(start, end).normalized()

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        size = min(abs(dx), abs(dy))
        fixed_end = QPointF(
            start.x() + (size if dx >= 0 else -size),
            start.y() + (size if dy >= 0 else -size),
        )
        return QRectF(start, fixed_end).normalized()


class PdfCorrectionEditor(QMainWindow):
    """
    Main PDF editor class.

    Parameters
    ----------
    input_path:
        PDF file to open.
    output_path:
        Output folder or a full output PDF file path. If a folder is supplied,
        the saved file is '<input_stem>_annotated.pdf' inside that folder.
    minimum_render_dpi:
        Minimum display render quality for visible PDF tiles. This is back to
        the previous high-quality default of 200 DPI for large A0/A1 drawings.
    maximum_render_dpi:
        Maximum display render quality when zoomed in. Only visible tiles are
        rendered, so large sheets remain safer than a single full-page pixmap.
    render_quality:
        Multiplier above the current screen zoom before selecting tile DPI.
        The previous crisp default is 2.0.
    """

    correction_saved = Signal(str)
    approve_requested = Signal()
    cancel_requested = Signal()

    def __init__(
        self,
        input_path: str | os.PathLike[str],
        output_path: str | os.PathLike[str],
        parent: Optional[QWidget] = None,
        *,
        minimum_render_dpi: float = 200.0,
        maximum_render_dpi: float = 600.0,
        render_quality: float = 2.0,
        cache_limit_mb: int = 384,
        max_visible_render_pixels: int = 90_000_000,
        tile_pixel_size: int = 1024,
        min_zoom: float = 0.02,
        max_zoom: float = 20.0,
        annotation_width_pt: float = 1.5,
        annotation_color: str = "#ff0000",
        default_text_size_pt: int = 12,
    ) -> None:
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("Create QApplication before creating PdfCorrectionEditor.")
        if parent is not None and not isinstance(parent, QWidget):
            raise TypeError("parent must be None or a QWidget/QMainWindow. Do not pass Ui_MainWindow as parent.")

        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.input_path = Path(input_path).expanduser().resolve()
        self.output_path = self._resolve_output_path(output_path)

        self.minimum_render_dpi = float(minimum_render_dpi)
        self.maximum_render_dpi = float(maximum_render_dpi)
        self.render_quality = float(render_quality)
        self.cache_limit_bytes = max(16, int(cache_limit_mb)) * 1024 * 1024
        self.max_visible_render_pixels = max(1_000_000, int(max_visible_render_pixels))
        self.tile_pixel_size = max(256, int(tile_pixel_size))
        self.min_zoom = float(min_zoom)
        self.max_zoom = float(max_zoom)
        self.current_zoom = 1.0
        self.current_render_dpi = self.minimum_render_dpi
        self.annotation_width_pt = float(annotation_width_pt)
        self.annotation_qcolor = QColor(annotation_color)
        self.annotation_rgb = self._qcolor_to_pymupdf_rgb(self.annotation_qcolor)
        self.default_text_size_pt = int(default_text_size_pt)

        self.document: Optional[Any] = None
        self.current_page_index = 0
        self.current_tool: Optional[str] = None
        self.page_scene_rect = QRectF()
        self._fit_mode = True
        self._next_annotation_id = 1
        self._updating_page_spinbox = False

        # Stored annotations for pages that are not currently loaded in the scene.
        self.annotations_by_page: Dict[int, List[Dict[str, Any]]] = {}
        self.undo_stack: List[Tuple[int, int]] = []  # (page_index, annotation_id)

        self.scene = QGraphicsScene(self)
        self.view = self._replace_ui_graphics_view()
        self.view.setScene(self.scene)

        self._page_background_item: Optional[QGraphicsRectItem] = None
        self._tile_items: List[QGraphicsPixmapItem] = []
        self._tile_cache: "OrderedDict[Tuple[Any, ...], Tuple[QPixmap, int]]" = OrderedDict()
        self._tile_cache_bytes = 0
        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self.update_visible_tiles)

        self.paper_size_label = QLabel(self)
        self.paper_size_label.setMinimumWidth(650)
        self.paper_size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.statusBar().addPermanentWidget(self.paper_size_label, 1)

        self._configure_ui()
        self._connect_signals()
        self._create_shortcuts()
        self.load_pdf(self.input_path)

    # ---------- UI setup ----------

    def _replace_ui_graphics_view(self) -> PdfGraphicsView:
        old_view = self.ui.graphicsView
        parent = old_view.parentWidget()
        self.ui.gridLayout.removeWidget(old_view)
        old_view.deleteLater()

        new_view = PdfGraphicsView(self, parent)
        new_view.setObjectName("graphicsView")
        self.ui.gridLayout.addWidget(new_view, 0, 0, 1, 1)
        self.ui.graphicsView = new_view
        return new_view

    def _configure_ui(self) -> None:
        self.setWindowTitle(f"PDF Correction Editor - {self.input_path.name}")
        if hasattr(self.ui, "viewerWindow"):
            self.ui.viewerWindow.setStyleSheet("background: #404040;")
        self.ui.graphicsView.setStyleSheet("QGraphicsView { background: #404040; border: 0px; }")
        self.scene.setBackgroundBrush(QBrush(QColor("#404040")))

        for name in ("btnCircle", "btnRectangle", "btnArrow", "btnText"):
            button = getattr(self.ui, name, None)
            if button is not None:
                button.setCheckable(True)

        if hasattr(self.ui, "spinBoxTextSize"):
            self.ui.spinBoxTextSize.setRange(1, 300)
            if self.ui.spinBoxTextSize.value() <= 0:
                self.ui.spinBoxTextSize.setValue(self.default_text_size_pt)
            self.ui.spinBoxTextSize.setSuffix(" pt")
            self.ui.spinBoxTextSize.setToolTip("Text size in PDF points")

        if hasattr(self.ui, "spinBoxPageNo"):
            self.ui.spinBoxPageNo.setRange(1, 1)
            self.ui.spinBoxPageNo.setToolTip("PDF page number")

        if hasattr(self.ui, "btnCorrection"):
            self.ui.btnCorrection.setToolTip("Save annotated PDF to the configured output path")

        self.view.horizontalScrollBar().valueChanged.connect(lambda _value: self.schedule_tile_update(35))
        self.view.verticalScrollBar().valueChanged.connect(lambda _value: self.schedule_tile_update(35))
        self.apply_cursor_for_tool()

    def _connect_signals(self) -> None:
        if hasattr(self.ui, "btnCircle"):
            self.ui.btnCircle.clicked.connect(lambda: self.set_tool(KIND_CIRCLE if self.current_tool != KIND_CIRCLE else None))
        if hasattr(self.ui, "btnRectangle"):
            self.ui.btnRectangle.clicked.connect(lambda: self.set_tool(KIND_RECTANGLE if self.current_tool != KIND_RECTANGLE else None))
        if hasattr(self.ui, "btnArrow"):
            self.ui.btnArrow.clicked.connect(lambda: self.set_tool(KIND_ARROW if self.current_tool != KIND_ARROW else None))
        if hasattr(self.ui, "btnText"):
            self.ui.btnText.clicked.connect(lambda: self.set_tool(KIND_TEXT if self.current_tool != KIND_TEXT else None))
        if hasattr(self.ui, "btnCorrection"):
            self.ui.btnCorrection.clicked.connect(self.save_correction)
        if hasattr(self.ui, "btnAppr"):
            self.ui.btnAppr.clicked.connect(self.approve_requested.emit)
        if hasattr(self.ui, "btnCancel"):
            self.ui.btnCancel.clicked.connect(self.cancel_requested.emit)
        if hasattr(self.ui, "spinBoxPageNo"):
            self.ui.spinBoxPageNo.valueChanged.connect(self._page_spinbox_changed)

    def _create_shortcuts(self) -> None:
        undo_action = QAction("Undo last annotation", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.triggered.connect(self.undo_last_draw)
        self.addAction(undo_action)

        delete_action = QAction("Delete selected annotations", self)
        delete_action.setShortcut(QKeySequence("Delete"))
        delete_action.triggered.connect(self.delete_selected_annotations)
        self.addAction(delete_action)

        zoom_in_action = QAction("Zoom in", self)
        zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in_action.triggered.connect(lambda: self.zoom_at_view_center(1.15))
        self.addAction(zoom_in_action)

        zoom_out_action = QAction("Zoom out", self)
        zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out_action.triggered.connect(lambda: self.zoom_at_view_center(1 / 1.15))
        self.addAction(zoom_out_action)

        fit_action = QAction("Fit page", self)
        fit_action.setShortcut(QKeySequence("Ctrl+0"))
        fit_action.triggered.connect(self.fit_page_to_view)
        self.addAction(fit_action)

    # ---------- Documents ----------

    def load_pdf(self, input_path: str | os.PathLike[str]) -> None:
        path = Path(input_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input PDF not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Input path must be a PDF file: {path}")

        if self.document is not None:
            self.document.close()

        self.document = fitz.open(str(path))
        if self.document.page_count == 0:
            self.document.close()
            self.document = None
            raise ValueError(f"PDF has no pages: {path}")

        self.input_path = path
        self.current_page_index = 0
        self.annotations_by_page.clear()
        self.undo_stack.clear()
        self._next_annotation_id = 1
        self._clear_tile_cache()
        self._update_page_spinbox_range()
        self.render_current_page(fit_after_render=True)
        self.statusBar().showMessage(f"Opened: {path}")

    def _resolve_output_path(self, output_path: str | os.PathLike[str]) -> Path:
        raw = Path(output_path).expanduser()
        if raw.suffix.lower() == ".pdf":
            target = raw
        else:
            stem = self.input_path.stem if hasattr(self, "input_path") else "annotated"
            target = raw / f"{stem}_annotated.pdf"
        target = target.resolve()

        try:
            if hasattr(self, "input_path") and target == self.input_path.resolve():
                target = target.with_name(f"{target.stem}_annotated.pdf")
        except FileNotFoundError:
            pass
        return target

    def save_correction(self) -> Optional[Path]:
        if self.document is None:
            QMessageBox.warning(self, "No PDF", "No PDF is loaded.")
            return None

        self.collect_current_page_annotations()
        target = self.output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_target = target.with_name(f".{target.stem}.tmp{target.suffix}")

        output_doc = fitz.open()
        try:
            output_doc.insert_pdf(self.document)
            self._write_annotations_to_pdf(output_doc)

            if temporary_target.exists():
                temporary_target.unlink()
            output_doc.save(str(temporary_target), garbage=4, deflate=True, clean=True)
            output_doc.close()
            os.replace(str(temporary_target), str(target))
        except Exception as exc:  # noqa: BLE001 - GUI error for any save failure.
            try:
                output_doc.close()
            except Exception:
                pass
            try:
                if temporary_target.exists():
                    temporary_target.unlink()
            except Exception:
                pass
            QMessageBox.critical(self, "Save failed", f"Could not save annotated PDF:\n{exc}")
            return None

        self.statusBar().showMessage(f"Saved annotated PDF: {target}")
        self.correction_saved.emit(str(target))
        QMessageBox.information(self, "Correction saved", f"Annotated PDF saved to:\n{target}")
        return target

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.document is not None:
            self.document.close()
            self.document = None
        self._clear_tile_cache()
        super().closeEvent(event)

    # ---------- Rendering ----------

    def render_current_page(self, *, fit_after_render: bool = False) -> None:
        if self.document is None:
            self.scene.clear()
            self.page_scene_rect = QRectF()
            self._update_status_bar()
            return

        self.scene.clear()
        self._tile_items.clear()
        page = self.document[self.current_page_index]
        self.page_scene_rect = QRectF(0, 0, float(page.rect.width), float(page.rect.height))
        self.scene.setSceneRect(self.page_scene_rect)

        self._page_background_item = QGraphicsRectItem(self.page_scene_rect)
        self._page_background_item.setData(ITEM_KIND_ROLE, KIND_PAGE_BACKGROUND)
        self._page_background_item.setPen(QPen(QColor("#bdbdbd"), 0.0))
        self._page_background_item.setBrush(QBrush(QColor("white")))
        self._page_background_item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self._page_background_item.setZValue(-200)
        self.scene.addItem(self._page_background_item)

        self._restore_page_annotations()
        self._update_page_spinbox_value()
        self._update_status_bar()

        if fit_after_render:
            self._fit_mode = True
            QTimer.singleShot(0, self.fit_page_to_view)
        else:
            self.schedule_tile_update(0)

    def schedule_tile_update(self, delay_ms: int = 35) -> None:
        if self.document is None or self.page_scene_rect.isNull():
            return
        self._tile_timer.start(max(0, int(delay_ms)))

    def update_visible_tiles(self) -> None:
        if self.document is None or self.page_scene_rect.isNull():
            return

        page = self.document[self.current_page_index]
        visible_rect = self._visible_page_rect()
        if visible_rect.isNull() or visible_rect.width() <= 0 or visible_rect.height() <= 0:
            return

        render_dpi = self._choose_render_dpi(visible_rect)
        self.current_render_dpi = render_dpi
        render_scale = render_dpi / PDF_POINTS_PER_INCH
        tile_scene_size = max(64.0, self.tile_pixel_size / render_scale)

        # Remove existing tile items but keep page background and annotations.
        for item in self._tile_items:
            if item.scene() is self.scene:
                self.scene.removeItem(item)
        self._tile_items.clear()

        x_start = math.floor(visible_rect.left() / tile_scene_size) * tile_scene_size
        y_start = math.floor(visible_rect.top() / tile_scene_size) * tile_scene_size
        x_end = math.ceil(visible_rect.right() / tile_scene_size) * tile_scene_size
        y_end = math.ceil(visible_rect.bottom() / tile_scene_size) * tile_scene_size

        x = x_start
        while x < x_end:
            y = y_start
            while y < y_end:
                clip = QRectF(x, y, tile_scene_size, tile_scene_size).intersected(self.page_scene_rect)
                if clip.width() > 0.5 and clip.height() > 0.5:
                    pixmap = self._render_tile_pixmap(page, clip, render_dpi)
                    item = QGraphicsPixmapItem(pixmap)
                    item.setData(ITEM_KIND_ROLE, KIND_PAGE_TILE)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                    item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                    item.setZValue(-100)
                    item.setPos(clip.left(), clip.top())
                    item.setScale(1.0 / render_scale)
                    self.scene.addItem(item)
                    self._tile_items.append(item)
                y += tile_scene_size
            x += tile_scene_size

        self._update_status_bar()

    def _visible_page_rect(self) -> QRectF:
        viewport_rect = self.view.viewport().rect()
        visible = self.view.mapToScene(viewport_rect).boundingRect().intersected(self.page_scene_rect)
        if visible.isNull():
            return visible

        # Prefetch around the viewport so panning does not immediately expose gray.
        pad_x = visible.width() * 0.15
        pad_y = visible.height() * 0.15
        visible.adjust(-pad_x, -pad_y, pad_x, pad_y)
        return visible.intersected(self.page_scene_rect)

    def _choose_render_dpi(self, visible_rect: QRectF) -> float:
        target = max(self.minimum_render_dpi, self.current_zoom * PDF_POINTS_PER_INCH * self.render_quality)
        target = min(self.maximum_render_dpi, target)

        visible_w_in = max(0.01, visible_rect.width() / PDF_POINTS_PER_INCH)
        visible_h_in = max(0.01, visible_rect.height() / PDF_POINTS_PER_INCH)
        pixels = visible_w_in * visible_h_in * target * target
        if pixels > self.max_visible_render_pixels:
            target *= math.sqrt(self.max_visible_render_pixels / pixels)
            # Allow reduction only when the visible region is too large. Keep it
            # as high as practical and never below 120 DPI unless absolutely huge.
            target = max(120.0, target)
        return max(72.0, min(self.maximum_render_dpi, target))

    def _render_tile_pixmap(self, page: Any, rect: QRectF, render_dpi: float) -> QPixmap:
        # Rounded coordinates give stable cache keys while keeping sub-point precision.
        key = (
            self.current_page_index,
            round(render_dpi, 1),
            round(rect.left(), 2),
            round(rect.top(), 2),
            round(rect.right(), 2),
            round(rect.bottom(), 2),
        )
        cached = self._tile_cache.get(key)
        if cached is not None:
            pixmap, byte_count = cached
            self._tile_cache.move_to_end(key)
            return pixmap

        render_scale = render_dpi / PDF_POINTS_PER_INCH
        matrix = fitz.Matrix(render_scale, render_scale)
        clip = fitz.Rect(float(rect.left()), float(rect.top()), float(rect.right()), float(rect.bottom()))
        pix = page.get_pixmap(matrix=matrix, clip=clip, alpha=False, colorspace=fitz.csRGB, annots=True)
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)
        byte_count = max(1, pixmap.width() * pixmap.height() * 4)
        self._tile_cache[key] = (pixmap, byte_count)
        self._tile_cache_bytes += byte_count
        self._trim_tile_cache()
        return pixmap

    def _trim_tile_cache(self) -> None:
        while self._tile_cache_bytes > self.cache_limit_bytes and self._tile_cache:
            _, (_, byte_count) = self._tile_cache.popitem(last=False)
            self._tile_cache_bytes -= byte_count

    def _clear_tile_cache(self) -> None:
        self._tile_cache.clear()
        self._tile_cache_bytes = 0

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._fit_mode and not self.page_scene_rect.isNull():
            QTimer.singleShot(0, self.fit_page_to_view)
        else:
            self.schedule_tile_update()

    def go_to_page(self, page_index: int) -> None:
        if self.document is None:
            return
        if page_index < 0 or page_index >= self.document.page_count:
            return
        if page_index == self.current_page_index:
            return
        self.collect_current_page_annotations()
        self.current_page_index = page_index
        self._clear_tile_cache()
        self.render_current_page(fit_after_render=True)

    def _page_spinbox_changed(self, value: int) -> None:
        if self._updating_page_spinbox:
            return
        self.go_to_page(int(value) - 1)

    def _update_page_spinbox_range(self) -> None:
        if self.document is None or not hasattr(self.ui, "spinBoxPageNo"):
            return
        self._updating_page_spinbox = True
        self.ui.spinBoxPageNo.setRange(1, max(1, int(self.document.page_count)))
        self.ui.spinBoxPageNo.setValue(self.current_page_index + 1)
        self._updating_page_spinbox = False

    def _update_page_spinbox_value(self) -> None:
        if not hasattr(self.ui, "spinBoxPageNo"):
            return
        self._updating_page_spinbox = True
        self.ui.spinBoxPageNo.setValue(self.current_page_index + 1)
        self._updating_page_spinbox = False

    # ---------- Zoom and pan ----------

    def fit_page_to_view(self) -> None:
        if self.page_scene_rect.isNull():
            return
        viewport_size = self.view.viewport().size()
        margin_px = 24
        available_w = max(1, viewport_size.width() - 2 * margin_px)
        available_h = max(1, viewport_size.height() - 2 * margin_px)
        zoom_x = available_w / self.page_scene_rect.width()
        zoom_y = available_h / self.page_scene_rect.height()
        self.current_zoom = max(self.min_zoom, min(self.max_zoom, min(zoom_x, zoom_y)))
        self.view.setTransform(QTransform().scale(self.current_zoom, self.current_zoom))
        self.view.centerOn(self.page_scene_rect.center())
        self._fit_mode = True
        self.schedule_tile_update(0)
        self._update_status_bar()

    def zoom_at_mouse(self, factor: float) -> None:
        self._fit_mode = False
        self._apply_zoom_factor(factor)

    def zoom_at_view_center(self, factor: float) -> None:
        self._fit_mode = False
        old_anchor = self.view.transformationAnchor()
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._apply_zoom_factor(factor)
        self.view.setTransformationAnchor(old_anchor)

    def _apply_zoom_factor(self, factor: float) -> None:
        new_zoom = max(self.min_zoom, min(self.max_zoom, self.current_zoom * factor))
        actual_factor = new_zoom / self.current_zoom if self.current_zoom else 1.0
        if abs(actual_factor - 1.0) < 0.0001:
            return
        self.view.scale(actual_factor, actual_factor)
        self.current_zoom = new_zoom
        self.schedule_tile_update(20)
        self._update_status_bar()

    # ---------- Tools ----------

    def set_tool(self, tool: Optional[str]) -> None:
        self.current_tool = tool
        if hasattr(self.ui, "btnCircle"):
            self.ui.btnCircle.setChecked(tool == KIND_CIRCLE)
        if hasattr(self.ui, "btnRectangle"):
            self.ui.btnRectangle.setChecked(tool == KIND_RECTANGLE)
        if hasattr(self.ui, "btnArrow"):
            self.ui.btnArrow.setChecked(tool == KIND_ARROW)
        if hasattr(self.ui, "btnText"):
            self.ui.btnText.setChecked(tool == KIND_TEXT)
        self.apply_cursor_for_tool()
        self._update_status_bar()

    def apply_cursor_for_tool(self) -> None:
        if self.current_tool in (KIND_CIRCLE, KIND_RECTANGLE, KIND_ARROW):
            self.view.setCursor(Qt.CursorShape.CrossCursor)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        elif self.current_tool == KIND_TEXT:
            self.view.setCursor(Qt.CursorShape.IBeamCursor)
            self.view.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.view.setCursor(Qt.CursorShape.ArrowCursor)
            self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)

    def _annotation_pen(self) -> QPen:
        pen = QPen(self.annotation_qcolor, self.annotation_width_pt)
        pen.setCosmetic(False)
        return pen

    def create_ellipse_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsEllipseItem:
        item = QGraphicsEllipseItem(rect.normalized())
        item.setPen(self._annotation_pen())
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._configure_annotation_item(item, KIND_CIRCLE, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    # Backward-compatible name if your older code calls create_circle_item.
    def create_circle_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsEllipseItem:
        return self.create_ellipse_item(rect, annotation_id)

    def create_rectangle_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsRectItem:
        item = QGraphicsRectItem(rect.normalized())
        item.setPen(self._annotation_pen())
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._configure_annotation_item(item, KIND_RECTANGLE, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def create_arrow_item(
        self,
        start: QPointF,
        end: QPointF,
        annotation_id: Optional[int] = None,
    ) -> ArrowGraphicsItem:
        item = ArrowGraphicsItem(start, end, self._annotation_pen(), head_length_pt=12.0)
        self._configure_annotation_item(item, KIND_ARROW, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def add_text_at(self, scene_pos: QPointF, text: Optional[str] = None) -> Optional[QGraphicsTextItem]:
        if text is None:
            text, ok = QInputDialog.getMultiLineText(self, "Add text", "Text:")
            if not ok:
                return None
        text = text.strip()
        if not text:
            return None

        font_size = max(1, int(self.ui.spinBoxTextSize.value())) if hasattr(self.ui, "spinBoxTextSize") else self.default_text_size_pt
        item = QGraphicsTextItem(text)
        font = QFont("Arial")
        # Scene units are PDF points, so use approximately the same scene size.
        font.setPixelSize(max(1, round(font_size)))
        item.setFont(font)
        item.setDefaultTextColor(self.annotation_qcolor)
        item.setTextWidth(-1)
        item.setPos(self.clamp_to_page(scene_pos))
        self._configure_annotation_item(item, KIND_TEXT)
        item.setData(FONT_SIZE_PT_ROLE, font_size)
        self.scene.addItem(item)
        self.register_new_annotation(item)
        return item

    def _configure_annotation_item(
        self,
        item: QGraphicsItem,
        kind: str,
        annotation_id: Optional[int] = None,
    ) -> None:
        if annotation_id is None:
            annotation_id = self._take_next_annotation_id()
        item.setData(ITEM_KIND_ROLE, kind)
        item.setData(ANNOTATION_ID_ROLE, int(annotation_id))
        item.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        item.setZValue(50)

    def _take_next_annotation_id(self) -> int:
        annotation_id = self._next_annotation_id
        self._next_annotation_id += 1
        return annotation_id

    def register_new_annotation(self, item: QGraphicsItem) -> None:
        annotation_id = int(item.data(ANNOTATION_ID_ROLE) or 0)
        if annotation_id <= 0:
            annotation_id = self._take_next_annotation_id()
            item.setData(ANNOTATION_ID_ROLE, annotation_id)
        self.undo_stack.append((self.current_page_index, annotation_id))
        self._update_status_bar()

    def undo_last_draw(self) -> None:
        if not self.undo_stack:
            self.statusBar().showMessage("Nothing to undo.")
            return
        page_index, annotation_id = self.undo_stack.pop()
        removed = False

        if page_index == self.current_page_index:
            item = self._find_scene_annotation(annotation_id)
            if item is not None:
                self.scene.removeItem(item)
                removed = True
        else:
            marks = self.annotations_by_page.get(page_index, [])
            before = len(marks)
            self.annotations_by_page[page_index] = [m for m in marks if int(m.get("id", 0)) != annotation_id]
            removed = before != len(self.annotations_by_page[page_index])

        self.statusBar().showMessage("Last annotation removed." if removed else "Could not find annotation to undo.")
        self._update_status_bar()

    def delete_selected_annotations(self) -> None:
        selected = [
            item for item in self.scene.selectedItems()
            if item.data(ITEM_KIND_ROLE) not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)
        ]
        if not selected:
            return
        selected_ids = {int(item.data(ANNOTATION_ID_ROLE) or 0) for item in selected}
        for item in selected:
            self.scene.removeItem(item)
        self.undo_stack = [entry for entry in self.undo_stack if entry[1] not in selected_ids]
        self.statusBar().showMessage("Selected annotation deleted.")
        self._update_status_bar()

    def _find_scene_annotation(self, annotation_id: int) -> Optional[QGraphicsItem]:
        for item in self.scene.items():
            if int(item.data(ANNOTATION_ID_ROLE) or 0) == annotation_id:
                return item
        return None

    def is_too_small_annotation(self, item: QGraphicsItem) -> bool:
        kind = item.data(ITEM_KIND_ROLE)
        if kind == KIND_CIRCLE and isinstance(item, QGraphicsEllipseItem):
            rect = item.mapRectToScene(item.rect()).normalized()
            return rect.width() < 2 or rect.height() < 2
        if kind == KIND_RECTANGLE and isinstance(item, QGraphicsRectItem):
            rect = item.mapRectToScene(item.rect()).normalized()
            return rect.width() < 2 or rect.height() < 2
        if kind == KIND_ARROW and isinstance(item, ArrowGraphicsItem):
            start = item.start_scene()
            end = item.end_scene()
            return math.hypot(end.x() - start.x(), end.y() - start.y()) < 2
        return False

    # ---------- Annotation persistence in memory ----------

    def collect_current_page_annotations(self) -> None:
        if self.document is None:
            return
        marks: List[Dict[str, Any]] = []
        overlay_items = sorted(
            (
                item for item in self.scene.items()
                if item.data(ITEM_KIND_ROLE) not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)
            ),
            key=lambda item: int(item.data(ANNOTATION_ID_ROLE) or 0),
        )
        for item in overlay_items:
            kind = item.data(ITEM_KIND_ROLE)
            annotation_id = int(item.data(ANNOTATION_ID_ROLE) or 0)

            if kind == KIND_CIRCLE and isinstance(item, QGraphicsEllipseItem):
                rect = item.mapRectToScene(item.rect()).normalized()
                marks.append(
                    {
                        "kind": KIND_CIRCLE,
                        "id": annotation_id,
                        "rect": (rect.left(), rect.top(), rect.right(), rect.bottom()),
                        "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt),
                    }
                )
            elif kind == KIND_RECTANGLE and isinstance(item, QGraphicsRectItem):
                rect = item.mapRectToScene(item.rect()).normalized()
                marks.append(
                    {
                        "kind": KIND_RECTANGLE,
                        "id": annotation_id,
                        "rect": (rect.left(), rect.top(), rect.right(), rect.bottom()),
                        "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt),
                    }
                )
            elif kind == KIND_ARROW and isinstance(item, ArrowGraphicsItem):
                start = item.start_scene()
                end = item.end_scene()
                marks.append(
                    {
                        "kind": KIND_ARROW,
                        "id": annotation_id,
                        "start": (start.x(), start.y()),
                        "end": (end.x(), end.y()),
                        "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt),
                    }
                )
            elif kind == KIND_TEXT and isinstance(item, QGraphicsTextItem):
                pos = item.scenePos()
                marks.append(
                    {
                        "kind": KIND_TEXT,
                        "id": annotation_id,
                        "pos": (pos.x(), pos.y()),
                        "text": item.toPlainText(),
                        "font_size_pt": int(
                            item.data(FONT_SIZE_PT_ROLE)
                            or (self.ui.spinBoxTextSize.value() if hasattr(self.ui, "spinBoxTextSize") else self.default_text_size_pt)
                        ),
                    }
                )
        self.annotations_by_page[self.current_page_index] = marks

    def _restore_page_annotations(self) -> None:
        max_id = self._next_annotation_id - 1
        for mark in self.annotations_by_page.get(self.current_page_index, []):
            kind = mark.get("kind")
            annotation_id = int(mark.get("id", 0))
            max_id = max(max_id, annotation_id)

            if kind == KIND_CIRCLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_ellipse_item(
                    QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))),
                    annotation_id=annotation_id,
                )
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_RECTANGLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_rectangle_item(
                    QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))),
                    annotation_id=annotation_id,
                )
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_ARROW:
                sx, sy = mark["start"]
                ex, ey = mark["end"]
                item = self.create_arrow_item(
                    QPointF(float(sx), float(sy)),
                    QPointF(float(ex), float(ey)),
                    annotation_id=annotation_id,
                )
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_TEXT:
                x, y = mark["pos"]
                item = QGraphicsTextItem(str(mark.get("text", "")))
                font_size = int(mark.get("font_size_pt", self.default_text_size_pt))
                font = QFont("Arial")
                font.setPixelSize(max(1, round(font_size)))
                item.setFont(font)
                item.setDefaultTextColor(self.annotation_qcolor)
                item.setTextWidth(-1)
                item.setPos(QPointF(float(x), float(y)))
                self._configure_annotation_item(item, KIND_TEXT, annotation_id=annotation_id)
                item.setData(FONT_SIZE_PT_ROLE, font_size)
                self.scene.addItem(item)

        self._next_annotation_id = max(self._next_annotation_id, max_id + 1)

    # ---------- Write annotations into PDF ----------

    def _write_annotations_to_pdf(self, output_doc: Any) -> None:
        for page_index, marks in self.annotations_by_page.items():
            if page_index < 0 or page_index >= output_doc.page_count:
                continue
            page = output_doc[page_index]
            for mark in marks:
                kind = mark.get("kind")
                if kind == KIND_CIRCLE:
                    self._write_ellipse(page, mark)
                elif kind == KIND_RECTANGLE:
                    self._write_rectangle(page, mark)
                elif kind == KIND_ARROW:
                    self._write_arrow(page, mark)
                elif kind == KIND_TEXT:
                    self._write_text(page, mark)

    def _mark_rect_to_pdf_rect(self, page: Any, mark: Dict[str, Any]) -> Any:
        x0, y0, x1, y1 = mark["rect"]
        corners = [
            QPointF(float(x0), float(y0)),
            QPointF(float(x1), float(y0)),
            QPointF(float(x1), float(y1)),
            QPointF(float(x0), float(y1)),
        ]
        points = [self.scene_point_to_pdf_point(page, p) for p in corners]
        xs = [p.x for p in points]
        ys = [p.y for p in points]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    def _write_ellipse(self, page: Any, mark: Dict[str, Any]) -> None:
        page.draw_oval(
            self._mark_rect_to_pdf_rect(page, mark),
            color=self.annotation_rgb,
            width=float(mark.get("pen_width_pt", self.annotation_width_pt)),
            overlay=True,
        )

    def _write_rectangle(self, page: Any, mark: Dict[str, Any]) -> None:
        page.draw_rect(
            self._mark_rect_to_pdf_rect(page, mark),
            color=self.annotation_rgb,
            width=float(mark.get("pen_width_pt", self.annotation_width_pt)),
            overlay=True,
        )

    def _write_arrow(self, page: Any, mark: Dict[str, Any]) -> None:
        sx, sy = mark["start"]
        ex, ey = mark["end"]
        start = self.scene_point_to_pdf_point(page, QPointF(float(sx), float(sy)))
        end = self.scene_point_to_pdf_point(page, QPointF(float(ex), float(ey)))
        width = float(mark.get("pen_width_pt", self.annotation_width_pt))

        page.draw_line(start, end, color=self.annotation_rgb, width=width, overlay=True)

        dx = end.x - start.x
        dy = end.y - start.y
        length = math.hypot(dx, dy)
        if length <= 0.01:
            return

        angle = math.atan2(dy, dx)
        arrow_angle = math.radians(28.0)
        head_len = max(8.0, width * 7.0)
        left = fitz.Point(end.x - head_len * math.cos(angle - arrow_angle), end.y - head_len * math.sin(angle - arrow_angle))
        right = fitz.Point(end.x - head_len * math.cos(angle + arrow_angle), end.y - head_len * math.sin(angle + arrow_angle))
        page.draw_line(end, left, color=self.annotation_rgb, width=width, overlay=True)
        page.draw_line(end, right, color=self.annotation_rgb, width=width, overlay=True)

    def _write_text(self, page: Any, mark: Dict[str, Any]) -> None:
        text = str(mark.get("text", "")).strip()
        if not text:
            return
        x, y = mark["pos"]
        font_size = int(mark.get("font_size_pt", self.default_text_size_pt))
        line_height_scene = font_size * 1.20

        for i, line in enumerate(text.splitlines()):
            if not line:
                continue
            baseline_scene = QPointF(float(x), float(y) + font_size + i * line_height_scene)
            pdf_point = self.scene_point_to_pdf_point(page, baseline_scene)
            page.insert_text(
                pdf_point,
                line,
                fontsize=font_size,
                fontname="helv",
                color=self.annotation_rgb,
                overlay=True,
            )

    def scene_point_to_pdf_point(self, page: Any, point: QPointF) -> Any:
        """Convert scene coordinates in displayed page points to PDF page points."""
        p = fitz.Point(float(point.x()), float(point.y()))
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
        if rotation:
            try:
                p = p * page.derotation_matrix
            except Exception:
                pass
        return p

    # ---------- Page geometry and status bar ----------

    def is_point_on_page(self, scene_pos: QPointF) -> bool:
        return self.page_scene_rect.contains(scene_pos)

    def clamp_to_page(self, scene_pos: QPointF) -> QPointF:
        if self.page_scene_rect.isNull():
            return QPointF(scene_pos)
        x = max(self.page_scene_rect.left(), min(scene_pos.x(), self.page_scene_rect.right()))
        y = max(self.page_scene_rect.top(), min(scene_pos.y(), self.page_scene_rect.bottom()))
        return QPointF(x, y)

    def _update_status_bar(self) -> None:
        if self.document is None:
            self.paper_size_label.setText("No PDF loaded")
            return
        page = self.document[self.current_page_index]
        page_text = f"Page {self.current_page_index + 1}/{self.document.page_count}"
        paper_text = self._paper_size_text(page)
        zoom_text = f"Zoom {self.current_zoom * 100:.0f}%"
        dpi_text = f"Render {self.current_render_dpi:.0f} DPI"
        tool_text = self.current_tool.title() if self.current_tool else "Select"
        self.paper_size_label.setText(f"{page_text} | {paper_text} | {zoom_text} | {dpi_text} | Tool: {tool_text}")

    def _paper_size_text(self, page: Any) -> str:
        width_pt = float(page.rect.width)
        height_pt = float(page.rect.height)
        width_mm = width_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        height_mm = height_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        name = self._match_paper_name(width_mm, height_mm)
        orientation = "Landscape" if width_mm > height_mm else "Portrait"
        width_in = width_mm / MM_PER_INCH
        height_in = height_mm / MM_PER_INCH
        return f"{name} {orientation} ({width_mm:.1f} x {height_mm:.1f} mm / {width_in:.2f} x {height_in:.2f} in)"

    @staticmethod
    def _match_paper_name(width_mm: float, height_mm: float) -> str:
        actual = sorted((width_mm, height_mm))
        best_name = "Custom"
        best_error = float("inf")
        for name, (w, h) in PAPER_SIZES_MM.items():
            expected = sorted((w, h))
            error = abs(actual[0] - expected[0]) + abs(actual[1] - expected[1])
            if error < best_error:
                best_name = name
                best_error = error
        return best_name if best_error <= PAPER_MATCH_TOLERANCE_MM * 2 else "Custom"

    @staticmethod
    def _qcolor_to_pymupdf_rgb(color: QColor) -> Tuple[float, float, float]:
        return (color.redF(), color.greenF(), color.blueF())


def main() -> int:
    app = QApplication(sys.argv)

    if len(sys.argv) >= 3:
        input_pdf = sys.argv[1]
        output_path = sys.argv[2]
    else:
        QMessageBox.information(
            None,
            "Usage",
            "Run with:\npython pdf_editor_controller.py input.pdf output_folder_or_output.pdf",
        )
        return 1

    editor = PdfCorrectionEditor(input_pdf, output_path)
    editor.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
