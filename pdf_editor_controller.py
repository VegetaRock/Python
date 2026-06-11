"""
PySide6 PDF correction editor for engineering drawings.

The viewer uses PDF points as scene coordinates and renders the visible PDF
area as screen-matched tiles.  This avoids the common A0/A1 problem where a
very high-DPI full-page pixmap is shrunk into the GraphicsView and thin CAD
lines become pale or blurry.

Install:
    pip install PySide6 PyMuPDF

Run:
    python pdf_editor_controller.py input.pdf output_folder_or_output.pdf
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
except ImportError:  # pragma: no cover
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
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QWidget,
)

try:
    from ui_pdfEditor import Ui_MainWindow
except ImportError:  # compatibility with the earlier generated file name
    from pdfEditorArhflp_ui import Ui_MainWindow  # type: ignore


ITEM_KIND_ROLE = 1001
ANNOTATION_ID_ROLE = 1002
FONT_SIZE_PT_ROLE = 1003
PEN_WIDTH_PT_ROLE = 1004
TEXT_REGISTERED_ROLE = 1005

KIND_PAGE_BACKGROUND = "page_background"
KIND_PAGE_TILE = "page_tile"
KIND_CIRCLE = "circle"          # ellipse/oval tool; button text may still say Circle
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
    """Selectable/movable arrow in PDF-point scene coordinates."""

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


class EditableTextGraphicsItem(QGraphicsTextItem):
    """Inline editable text annotation with a visible edit box."""

    def __init__(
        self,
        editor: "PdfCorrectionEditor",
        text: str = "",
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(text, parent)
        self.editor = editor

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        super().focusOutEvent(event)
        self.editor.finish_text_item_edit(self, discard_empty=True)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.editor.finish_text_item_edit(self, discard_empty=True)
            self.editor.set_tool(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[override]
        super().paint(painter, option, widget)
        if self.hasFocus() or self.isSelected() or not self.toPlainText().strip():
            painter.save()
            pen = QPen(self.defaultTextColor(), 0)
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5))
            painter.restore()


class PdfGraphicsView(QGraphicsView):
    """QGraphicsView with wheel zoom, middle-button pan, and drawing tools."""

    def __init__(self, editor: "PdfCorrectionEditor", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self._drawing_start: Optional[QPointF] = None
        self._temp_item: Optional[QGraphicsItem] = None
        self._panning = False
        self._pan_start = QPoint()
        self._pan_h_value = 0
        self._pan_v_value = 0

        # Do not enable SmoothPixmapTransform for PDF tiles.  For A0/A1 sheets,
        # Qt interpolation makes thin CAD lines look pale when the page is fit
        # into the view.  Tiles are rendered at the screen pixel density instead.
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
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
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        delta = event.angleDelta().y()
        if delta == 0:
            delta = event.pixelDelta().y()
        if delta == 0:
            event.ignore()
            return
        factor = 1.15 ** (delta / 120.0)
        self.editor.zoom_at_mouse(factor)
        event.accept()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event.position().toPoint())
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self.editor.is_text_item_being_edited():
            focused = self.editor.current_text_editor()
            clicked = self.itemAt(event.position().toPoint())
            if clicked is focused:
                super().mousePressEvent(event)
                return
            self.editor.finish_current_text_edit(discard_empty=True)

        if event.button() == Qt.MouseButton.LeftButton and self.editor.current_tool:
            scene_pos = self.mapToScene(event.position().toPoint())
            if not self.editor.is_point_on_page(scene_pos):
                event.accept()
                return
            scene_pos = self.editor.clamp_to_page(scene_pos)
            tool = self.editor.current_tool

            if tool == KIND_CIRCLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_ellipse_item(QRectF(scene_pos, scene_pos))
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if tool == KIND_RECTANGLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_rectangle_item(QRectF(scene_pos, scene_pos))
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if tool == KIND_ARROW:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_arrow_item(scene_pos, scene_pos)
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if tool == KIND_TEXT:
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
            force_square = bool(
                QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            tool = self.editor.current_tool

            if tool == KIND_CIRCLE and isinstance(self._temp_item, QGraphicsEllipseItem):
                self._temp_item.setRect(self._drag_rect(self._drawing_start, scene_pos, force_square))
                event.accept()
                return

            if tool == KIND_RECTANGLE and isinstance(self._temp_item, QGraphicsRectItem):
                self._temp_item.setRect(self._drag_rect(self._drawing_start, scene_pos, force_square))
                event.accept()
                return

            if tool == KIND_ARROW and isinstance(self._temp_item, ArrowGraphicsItem):
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

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, QGraphicsTextItem) and item.data(ITEM_KIND_ROLE) == KIND_TEXT:
            self.editor.begin_text_item_edit(item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        # Escape always finishes current text edit and unselects the active tool.
        if event.key() == Qt.Key.Key_Escape:
            self.editor.finish_current_text_edit(discard_empty=True)
            self.editor.set_tool(None)
            self.scene().clearSelection()
            event.accept()
            return

        # When a QGraphicsTextItem is being edited, do not intercept Backspace,
        # Delete, Ctrl+Z, cursor keys, etc.  Let the text editor handle them.
        if self.editor.is_text_item_being_edited():
            super().keyPressEvent(event)
            return

        if event.matches(QKeySequence.StandardKey.Undo):
            self.editor.undo_last_draw()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.editor.delete_selected_annotations()
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
        side = min(abs(dx), abs(dy))
        fixed_end = QPointF(
            start.x() + (side if dx >= 0 else -side),
            start.y() + (side if dy >= 0 else -side),
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
        Output folder or a full output PDF file path.  If a folder is supplied,
        the saved file is '<input_stem>_annotated.pdf' inside that folder.
    screen_matched_rendering:
        Keep display tiles close to the final viewport pixel size.  This is the
        important part for Acrobat-like A0/A1 fit-to-page clarity; high-DPI
        oversampling followed by downscaling makes thin lines look pale.
    graphics_min_line_width_px:
        MuPDF thin-line enhancement in screen pixels.  1.0 is close to Acrobat's
        engineering drawing display behavior.  Use 0.0 to disable.
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
        minimum_render_dpi: float = 24.0,
        maximum_render_dpi: float = 600.0,
        render_quality: float = 1.0,
        screen_matched_rendering: bool = True,
        graphics_min_line_width_px: float = 1.0,
        antialias_level: int = 8,
        cache_limit_mb: int = 512,
        max_visible_render_pixels: int = 160_000_000,
        tile_pixel_size: int = 2048,
        min_zoom: float = 0.01,
        max_zoom: float = 40.0,
        initial_view: str = "fit_page",  # "fit_page", "fit_width", "actual_size", or "auto"
        use_smooth_pixmap: bool = False,
        annotation_width_pt: float = 1.5,
        annotation_color: str = "#ff0000",
        default_text_size_pt: int = 12,
        auto_text_size_by_paper: bool = True,
        base_text_size_a4_pt: float = 15.0,
        min_auto_text_size_pt: int = 8,
        max_auto_text_size_pt: int = 90,
        auto_text_size: Optional[bool] = None,
        a4_text_size_pt: Optional[float] = None,
    ) -> None:
        if QApplication.instance() is None:
            raise RuntimeError("Create QApplication before creating PdfCorrectionEditor.")
        if parent is not None and not isinstance(parent, QWidget):
            raise TypeError("parent must be None or QWidget/QMainWindow. Do not pass Ui_MainWindow.")

        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        self.input_path = Path(input_path).expanduser().resolve()
        self.output_path = self._resolve_output_path(output_path)

        self.minimum_render_dpi = float(minimum_render_dpi)
        self.maximum_render_dpi = float(maximum_render_dpi)
        self.render_quality = max(0.25, float(render_quality))
        self.screen_matched_rendering = bool(screen_matched_rendering)
        self.graphics_min_line_width_px = max(0.0, float(graphics_min_line_width_px))
        self.antialias_level = max(0, int(antialias_level))
        self._apply_mupdf_rendering_hints()

        self.cache_limit_bytes = max(16, int(cache_limit_mb)) * 1024 * 1024
        self.max_visible_render_pixels = max(1_000_000, int(max_visible_render_pixels))
        self.tile_pixel_size = max(256, int(tile_pixel_size))
        self.min_zoom = float(min_zoom)
        self.max_zoom = float(max_zoom)
        self.initial_view = initial_view
        self.use_smooth_pixmap = bool(use_smooth_pixmap)

        self.current_zoom: float = 1.0
        self.current_render_dpi: float = self.minimum_render_dpi
        self.annotation_width_pt = float(annotation_width_pt)
        self.annotation_qcolor = QColor(annotation_color)
        self.annotation_rgb = self._qcolor_to_pymupdf_rgb(self.annotation_qcolor)
        self.default_text_size_pt = int(default_text_size_pt)
        if auto_text_size is not None:
            auto_text_size_by_paper = bool(auto_text_size)
        if a4_text_size_pt is not None:
            base_text_size_a4_pt = float(a4_text_size_pt)
        self.auto_text_size_by_paper = bool(auto_text_size_by_paper)
        self.base_text_size_a4_pt = float(base_text_size_a4_pt)
        self.min_auto_text_size_pt = int(min_auto_text_size_pt)
        self.max_auto_text_size_pt = int(max_auto_text_size_pt)

        self.document: Optional[Any] = None
        self.current_page_index = 0
        self.current_tool: Optional[str] = None
        self.page_scene_rect = QRectF()
        self._fit_mode: str = initial_view
        self._next_annotation_id = 1
        self._updating_page_spinbox = False

        self.annotations_by_page: Dict[int, List[Dict[str, Any]]] = {}
        self.undo_stack: List[Tuple[int, int]] = []

        self.scene = QGraphicsScene(self)
        self.view = self._replace_ui_graphics_view()
        self.view.setScene(self.scene)

        self._page_background_item: Optional[QGraphicsRectItem] = None
        self._tile_items: List[QGraphicsPixmapItem] = []
        self._tile_cache: OrderedDict[Tuple[Any, ...], Tuple[QPixmap, int]] = OrderedDict()
        self._tile_cache_bytes = 0

        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self.update_visible_tiles)

        self.paper_size_label = QLabel(self)
        self.paper_size_label.setMinimumWidth(640)
        self.paper_size_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.statusBar().addPermanentWidget(self.paper_size_label, 1)

        self._configure_ui()
        self._connect_signals()
        self._create_shortcuts()
        self.load_pdf(self.input_path)

    # ------------------------------------------------------------------
    # Rendering configuration
    # ------------------------------------------------------------------

    def _apply_mupdf_rendering_hints(self) -> None:
        """Apply MuPDF rendering hints for CAD / engineering drawings."""
        try:
            fitz.TOOLS.set_aa_level(self.antialias_level)
        except Exception:
            pass
        try:
            fitz.TOOLS.set_graphics_min_line_width(self.graphics_min_line_width_px)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _replace_ui_graphics_view(self) -> PdfGraphicsView:
        old_view = self.ui.graphicsView
        parent_widget = old_view.parentWidget()
        self.ui.gridLayout.removeWidget(old_view)
        old_view.deleteLater()
        new_view = PdfGraphicsView(self, parent_widget)
        new_view.setObjectName("graphicsView")
        self.ui.gridLayout.addWidget(new_view, 0, 0, 1, 1)
        self.ui.graphicsView = new_view
        return new_view

    def _configure_ui(self) -> None:
        self.setWindowTitle(f"PDF Correction Editor - {self.input_path.name}")

        # Make graphicsView as large as possible.  Every extra viewport pixel
        # directly improves A0/A1 fit-to-page detail.
        for layout_name in ("verticalLayout", "gridLayout", "horizontalLayout", "horizontalLayout_2"):
            layout = getattr(self.ui, layout_name, None)
            if layout is not None:
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(4 if layout_name.startswith("horizontal") else 0)

        if hasattr(self.ui, "viewerWindow"):
            self.ui.viewerWindow.setStyleSheet("background: #404040;")
        self.ui.graphicsView.setStyleSheet("QGraphicsView { background: #404040; border: 0px; }")
        self.scene.setBackgroundBrush(QBrush(QColor("#404040")))

        for name in ("btnCircle", "btnRectangle", "btnArrow", "btnText"):
            btn = getattr(self.ui, name, None)
            if btn is not None:
                btn.setCheckable(True)

        if hasattr(self.ui, "spinBoxTextSize"):
            self.ui.spinBoxTextSize.setRange(1, 300)
            if self.ui.spinBoxTextSize.value() <= 0:
                self.ui.spinBoxTextSize.setValue(self.default_text_size_pt)
            self.ui.spinBoxTextSize.setSuffix(" pt")

        if hasattr(self.ui, "spinBoxPageNo"):
            self.ui.spinBoxPageNo.setRange(1, 1)
            self.ui.spinBoxPageNo.setToolTip("PDF page number")

        if hasattr(self.ui, "btnCorrection"):
            self.ui.btnCorrection.setToolTip("Save annotated PDF to output path")

        self.view.horizontalScrollBar().valueChanged.connect(lambda _: self.schedule_tile_update(20))
        self.view.verticalScrollBar().valueChanged.connect(lambda _: self.schedule_tile_update(20))
        self.apply_cursor_for_tool()

    def _connect_signals(self) -> None:
        def toggle(kind: str):
            return lambda: self.set_tool(kind if self.current_tool != kind else None)

        if hasattr(self.ui, "btnCircle"):
            self.ui.btnCircle.clicked.connect(toggle(KIND_CIRCLE))
        if hasattr(self.ui, "btnRectangle"):
            self.ui.btnRectangle.clicked.connect(toggle(KIND_RECTANGLE))
        if hasattr(self.ui, "btnArrow"):
            self.ui.btnArrow.clicked.connect(toggle(KIND_ARROW))
        if hasattr(self.ui, "btnText"):
            self.ui.btnText.clicked.connect(toggle(KIND_TEXT))
        if hasattr(self.ui, "btnCorrection"):
            self.ui.btnCorrection.clicked.connect(self.save_correction)
        if hasattr(self.ui, "btnAppr"):
            self.ui.btnAppr.clicked.connect(self.approve_requested.emit)
        if hasattr(self.ui, "btnCancel"):
            self.ui.btnCancel.clicked.connect(self.cancel_requested.emit)
        if hasattr(self.ui, "spinBoxPageNo"):
            self.ui.spinBoxPageNo.valueChanged.connect(self._page_spinbox_changed)

    def _create_shortcuts(self) -> None:
        shortcuts: List[Tuple[str | QKeySequence.StandardKey, Any]] = [
            (QKeySequence.StandardKey.ZoomIn, lambda: self.zoom_at_view_center(1.15)),
            (QKeySequence.StandardKey.ZoomOut, lambda: self.zoom_at_view_center(1.0 / 1.15)),
            (QKeySequence("Ctrl+0"), self.fit_page_to_view),
            (QKeySequence("Ctrl+W"), self.fit_width_to_view),
            (QKeySequence("Ctrl+1"), self.actual_size_view),
        ]
        for key, slot in shortcuts:
            action = QAction(self)
            action.setShortcut(key)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(slot)
            self.addAction(action)

    # ------------------------------------------------------------------
    # Document lifecycle
    # ------------------------------------------------------------------

    def load_pdf(self, input_path: str | os.PathLike[str]) -> None:
        path = Path(input_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input PDF not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Input path must be a PDF file: {path}")

        if self.document is not None:
            self.document.close()
            self.document = None

        doc = fitz.open(str(path))
        if doc.page_count == 0:
            doc.close()
            raise ValueError(f"PDF has no pages: {path}")

        self.document = doc
        self.input_path = path
        self.current_page_index = 0
        self.annotations_by_page.clear()
        self.undo_stack.clear()
        self._next_annotation_id = 1
        self._clear_tile_cache()
        self._update_page_spinbox_range()
        self.render_current_page(fit_after_render=True)
        self.statusBar().showMessage(f"Opened: {path}", 5000)

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
                target = target.with_name(f"{target.stem}_out.pdf")
        except (OSError, ValueError):
            pass
        return target

    def save_correction(self) -> Optional[Path]:
        if self.document is None:
            QMessageBox.warning(self, "No PDF loaded", "Please open a PDF first.")
            return None

        self.finish_current_text_edit(discard_empty=True)
        self.collect_current_page_annotations()
        target = self.output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f".{target.stem}.tmp{target.suffix}")

        output_doc = fitz.open()
        try:
            output_doc.insert_pdf(self.document)
            self._write_annotations_to_pdf(output_doc)
            if tmp_path.exists():
                tmp_path.unlink()
            output_doc.save(str(tmp_path), garbage=4, deflate=True, clean=True)
            output_doc.close()
            os.replace(str(tmp_path), str(target))
        except Exception as exc:  # noqa: BLE001
            try:
                output_doc.close()
            except Exception:
                pass
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            QMessageBox.critical(self, "Save failed", f"Could not save annotated PDF:\n{exc}")
            return None

        self.statusBar().showMessage(f"Saved: {target}", 8000)
        self.correction_saved.emit(str(target))
        QMessageBox.information(self, "Correction saved", f"Annotated PDF saved to:\n{target}")
        return target

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._tile_timer.stop()
        self._clear_tile_cache()
        if self.document is not None:
            self.document.close()
            self.document = None
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_current_page(self, *, fit_after_render: bool = False) -> None:
        if self.document is None:
            self.scene.clear()
            self.page_scene_rect = QRectF()
            self._update_status_bar()
            return

        self.scene.clear()
        self._tile_items.clear()
        page = self.document[self.current_page_index]
        self.page_scene_rect = QRectF(0.0, 0.0, float(page.rect.width), float(page.rect.height))
        self.scene.setSceneRect(self.page_scene_rect)

        bg = QGraphicsRectItem(self.page_scene_rect)
        bg.setData(ITEM_KIND_ROLE, KIND_PAGE_BACKGROUND)
        bg.setPen(QPen(QColor("#bdbdbd"), 0.0))
        bg.setBrush(QBrush(Qt.GlobalColor.white))
        bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        bg.setZValue(-200)
        self.scene.addItem(bg)
        self._page_background_item = bg

        self._restore_page_annotations()
        self._update_auto_text_size_for_current_page()
        self._update_page_spinbox_value()
        self._update_status_bar()

        if fit_after_render:
            mode = (self.initial_view or "fit_page").lower()
            if mode == "actual_size":
                self._fit_mode = "manual"
                QTimer.singleShot(0, self.actual_size_view)
            elif mode in ("fit_width", "auto"):
                self._fit_mode = "fit_width"
                QTimer.singleShot(0, self.fit_width_to_view)
            else:
                self._fit_mode = "fit_page"
                QTimer.singleShot(0, self.fit_page_to_view)
        else:
            self.schedule_tile_update(0)

    def schedule_tile_update(self, delay_ms: int = 25) -> None:
        if self.document is None or self.page_scene_rect.isNull():
            return
        self._tile_timer.start(max(0, int(delay_ms)))

    def update_visible_tiles(self) -> None:
        if self.document is None or self.page_scene_rect.isNull():
            return
        page = self.document[self.current_page_index]
        visible = self._visible_page_rect()
        if visible.isNull() or visible.width() <= 0 or visible.height() <= 0:
            return

        render_dpi = self._choose_render_dpi(visible)
        self.current_render_dpi = render_dpi
        render_scale = render_dpi / PDF_POINTS_PER_INCH
        tile_scene_size = max(64.0, self.tile_pixel_size / render_scale)

        for item in self._tile_items:
            if item.scene() is self.scene:
                self.scene.removeItem(item)
        self._tile_items.clear()

        x0 = math.floor(visible.left() / tile_scene_size) * tile_scene_size
        y0 = math.floor(visible.top() / tile_scene_size) * tile_scene_size
        x1 = math.ceil(visible.right() / tile_scene_size) * tile_scene_size
        y1 = math.ceil(visible.bottom() / tile_scene_size) * tile_scene_size

        x = x0
        while x < x1:
            y = y0
            while y < y1:
                clip = QRectF(x, y, tile_scene_size, tile_scene_size).intersected(self.page_scene_rect)
                if clip.width() > 0.5 and clip.height() > 0.5:
                    pixmap = self._render_tile_pixmap(page, clip, render_dpi)
                    item = QGraphicsPixmapItem(pixmap)
                    item.setData(ITEM_KIND_ROLE, KIND_PAGE_TILE)
                    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                    item.setTransformationMode(
                        Qt.TransformationMode.SmoothTransformation
                        if self.use_smooth_pixmap
                        else Qt.TransformationMode.FastTransformation
                    )
                    item.setZValue(-100)
                    item.setPos(clip.left(), clip.top())
                    item.setScale(1.0 / render_scale)
                    self.scene.addItem(item)
                    self._tile_items.append(item)
                y += tile_scene_size
            x += tile_scene_size

        self._update_status_bar()

    def _visible_page_rect(self) -> QRectF:
        visible = self.view.mapToScene(self.view.viewport().rect()).boundingRect().intersected(
            self.page_scene_rect
        )
        if visible.isNull():
            return visible
        visible.adjust(-visible.width() * 0.20, -visible.height() * 0.20,
                       visible.width() * 0.20, visible.height() * 0.20)
        return visible.intersected(self.page_scene_rect)

    def _choose_render_dpi(self, visible_rect: QRectF) -> float:
        """
        Choose tile DPI for the current view.

        In screen-matched mode, DPI follows the viewport transform instead of a
        large fixed floor.  Example: a full A0 sheet fitted into a 1400 px wide
        view may only need about 30 screen DPI.  Rendering that same page tile
        at 240 DPI and shrinking it to 30 DPI is exactly what makes linework
        pale.  MuPDF's minimum graphics line width keeps hairlines visible at
        the screen pixel grid.
        """
        zoom_x = abs(float(self.view.transform().m11())) or self.current_zoom or 1.0
        zoom_y = abs(float(self.view.transform().m22())) or self.current_zoom or 1.0
        screen_dpi = max(zoom_x, zoom_y) * PDF_POINTS_PER_INCH * self._device_pixel_ratio()

        if self.screen_matched_rendering:
            target = screen_dpi * self.render_quality
            # Deliberately do not force the old high minimum here.  Full-sheet
            # engineering drawings look better when rasterized at the final
            # screen size plus MuPDF thin-line enhancement.
            target = max(8.0, target)
        else:
            target = max(self.minimum_render_dpi, screen_dpi * self.render_quality)

        target = min(self.maximum_render_dpi, target)

        # Safety cap based on visible area only.
        vis_w_in = max(0.001, visible_rect.width() / PDF_POINTS_PER_INCH)
        vis_h_in = max(0.001, visible_rect.height() / PDF_POINTS_PER_INCH)
        est_pixels = vis_w_in * vis_h_in * target * target
        if est_pixels > self.max_visible_render_pixels:
            target = math.sqrt(self.max_visible_render_pixels / (vis_w_in * vis_h_in))
            target = min(self.maximum_render_dpi, max(8.0, target))

        return float(max(8.0, target))

    def _device_pixel_ratio(self) -> float:
        screen = self.screen()
        if screen is not None:
            return max(1.0, float(screen.devicePixelRatio()))
        app = QApplication.instance()
        if app is not None and app.screens():
            return max(1.0, float(app.screens()[0].devicePixelRatio()))
        return 1.0

    def _render_tile_pixmap(self, page: Any, rect: QRectF, render_dpi: float) -> QPixmap:
        key = (
            self.current_page_index,
            int(getattr(page, "rotation", 0) or 0),
            round(render_dpi, 2),
            round(self.graphics_min_line_width_px, 2),
            self.antialias_level,
            round(rect.left(), 2), round(rect.top(), 2),
            round(rect.right(), 2), round(rect.bottom(), 2),
        )
        cached = self._tile_cache.get(key)
        if cached is not None:
            self._tile_cache.move_to_end(key)
            return cached[0]

        self._apply_mupdf_rendering_hints()
        render_scale = render_dpi / PDF_POINTS_PER_INCH
        matrix = fitz.Matrix(render_scale, render_scale)
        clip = fitz.Rect(float(rect.left()), float(rect.top()), float(rect.right()), float(rect.bottom()))
        pix = page.get_pixmap(
            matrix=matrix,
            clip=clip,
            alpha=False,
            colorspace=fitz.csRGB,
            annots=False,
        )
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(image)

        bytes_used = max(1, pixmap.width() * pixmap.height() * 4)
        self._tile_cache[key] = (pixmap, bytes_used)
        self._tile_cache_bytes += bytes_used
        self._evict_tile_cache()
        return pixmap

    def _evict_tile_cache(self) -> None:
        while self._tile_cache_bytes > self.cache_limit_bytes and self._tile_cache:
            _, (_, bytes_used) = self._tile_cache.popitem(last=False)
            self._tile_cache_bytes -= bytes_used

    def _clear_tile_cache(self) -> None:
        self._tile_cache.clear()
        self._tile_cache_bytes = 0

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self.document is None or self.page_scene_rect.isNull():
            return
        if self._fit_mode == "fit_page":
            QTimer.singleShot(0, self.fit_page_to_view)
        elif self._fit_mode == "fit_width":
            QTimer.singleShot(0, self.fit_width_to_view)
        else:
            self.schedule_tile_update(40)

    # ------------------------------------------------------------------
    # Text size helpers
    # ------------------------------------------------------------------

    def _update_auto_text_size_for_current_page(self) -> None:
        if not self.auto_text_size_by_paper or self.document is None:
            return
        if not hasattr(self.ui, "spinBoxTextSize"):
            return
        page = self.document[self.current_page_index]
        size = self._auto_text_size_for_page(page)
        old_block = self.ui.spinBoxTextSize.blockSignals(True)
        self.ui.spinBoxTextSize.setValue(size)
        self.ui.spinBoxTextSize.blockSignals(old_block)

    def _auto_text_size_for_page(self, page: Any) -> int:
        w_mm = float(page.rect.width) * MM_PER_INCH / PDF_POINTS_PER_INCH
        h_mm = float(page.rect.height) * MM_PER_INCH / PDF_POINTS_PER_INCH
        a4_area = 210.0 * 297.0
        page_area = max(1.0, w_mm * h_mm)
        scale = math.sqrt(page_area / a4_area)
        value = round(self.base_text_size_a4_pt * scale)
        value = max(self.min_auto_text_size_pt, min(self.max_auto_text_size_pt, int(value)))
        return value

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    def go_to_page(self, page_index: int) -> None:
        if self.document is None:
            return
        if not (0 <= page_index < self.document.page_count):
            return
        if page_index == self.current_page_index:
            return
        self.finish_current_text_edit(discard_empty=True)
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
        self.ui.spinBoxPageNo.setRange(1, max(1, self.document.page_count))
        self.ui.spinBoxPageNo.setValue(self.current_page_index + 1)
        self._updating_page_spinbox = False

    def _update_page_spinbox_value(self) -> None:
        if not hasattr(self.ui, "spinBoxPageNo"):
            return
        self._updating_page_spinbox = True
        self.ui.spinBoxPageNo.setValue(self.current_page_index + 1)
        self._updating_page_spinbox = False

    # ------------------------------------------------------------------
    # Zoom and view modes
    # ------------------------------------------------------------------

    def fit_page_to_view(self) -> None:
        if self.page_scene_rect.isNull():
            return
        vp = self.view.viewport().size()
        margin = 4
        zoom_x = max(1, vp.width() - 2 * margin) / self.page_scene_rect.width()
        zoom_y = max(1, vp.height() - 2 * margin) / self.page_scene_rect.height()
        self._set_zoom(min(zoom_x, zoom_y), center_page=True)
        self._fit_mode = "fit_page"

    def fit_width_to_view(self) -> None:
        if self.page_scene_rect.isNull():
            return
        vp = self.view.viewport().size()
        margin = 4
        zoom = max(1, vp.width() - 2 * margin) / self.page_scene_rect.width()
        self._set_zoom(zoom, center_page=True)
        top_center_y = self.page_scene_rect.top() + vp.height() / max(2 * self.current_zoom, 0.001)
        self.view.centerOn(self.page_scene_rect.center().x(), top_center_y)
        self._fit_mode = "fit_width"

    def actual_size_view(self) -> None:
        self._set_zoom(1.0, center_page=True)
        self._fit_mode = "manual"

    def zoom_at_mouse(self, factor: float) -> None:
        self._fit_mode = "manual"
        self._apply_zoom_factor(factor)

    def zoom_at_view_center(self, factor: float) -> None:
        self._fit_mode = "manual"
        old_anchor = self.view.transformationAnchor()
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._apply_zoom_factor(factor)
        self.view.setTransformationAnchor(old_anchor)

    def _apply_zoom_factor(self, factor: float) -> None:
        if self.current_zoom <= 0:
            self.current_zoom = 1.0
        new_zoom = max(self.min_zoom, min(self.max_zoom, self.current_zoom * factor))
        actual = new_zoom / self.current_zoom
        if abs(actual - 1.0) < 1e-4:
            return
        self.view.scale(actual, actual)
        self.current_zoom = new_zoom
        self.schedule_tile_update(10)
        self._update_status_bar()

    def _set_zoom(self, zoom: float, *, center_page: bool = False) -> None:
        self.current_zoom = max(self.min_zoom, min(self.max_zoom, float(zoom)))
        self.view.setTransform(QTransform().scale(self.current_zoom, self.current_zoom))
        if center_page:
            self.view.centerOn(self.page_scene_rect.center())
        self.schedule_tile_update(0)
        self._update_status_bar()

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def set_tool(self, tool: Optional[str]) -> None:
        if tool != KIND_TEXT:
            self.finish_current_text_edit(discard_empty=True)
        self.current_tool = tool
        for name, kind in (
            ("btnCircle", KIND_CIRCLE),
            ("btnRectangle", KIND_RECTANGLE),
            ("btnArrow", KIND_ARROW),
            ("btnText", KIND_TEXT),
        ):
            btn = getattr(self.ui, name, None)
            if btn is not None:
                btn.setChecked(tool == kind)
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
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def create_ellipse_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsEllipseItem:
        item = QGraphicsEllipseItem(rect.normalized())
        item.setPen(self._annotation_pen())
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._configure_annotation_item(item, KIND_CIRCLE, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def create_circle_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsEllipseItem:
        return self.create_ellipse_item(rect, annotation_id)

    def create_rectangle_item(self, rect: QRectF, annotation_id: Optional[int] = None) -> QGraphicsRectItem:
        item = QGraphicsRectItem(rect.normalized())
        item.setPen(self._annotation_pen())
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._configure_annotation_item(item, KIND_RECTANGLE, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def create_arrow_item(self, start: QPointF, end: QPointF, annotation_id: Optional[int] = None) -> ArrowGraphicsItem:
        diag = math.hypot(self.page_scene_rect.width(), self.page_scene_rect.height())
        head_len = max(12.0, diag * 0.008)
        item = ArrowGraphicsItem(start, end, self._annotation_pen(), head_len)
        self._configure_annotation_item(item, KIND_ARROW, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def add_text_at(self, scene_pos: QPointF, text: Optional[str] = None) -> Optional[QGraphicsTextItem]:
        """Create an inline editable text box exactly at the clicked PDF point."""
        font_size = self.current_text_size_pt()
        item = EditableTextGraphicsItem(self, text or "")
        font = QFont("Arial")
        font.setPointSizeF(float(font_size))
        item.setFont(font)
        item.setDefaultTextColor(self.annotation_qcolor)
        item.setTextWidth(self._default_text_box_width_pt(font_size))
        item.setPos(self.clamp_to_page(scene_pos))
        self._configure_annotation_item(item, KIND_TEXT)
        item.setData(FONT_SIZE_PT_ROLE, font_size)
        item.setData(TEXT_REGISTERED_ROLE, False)
        self.scene.addItem(item)
        self.register_new_annotation(item)
        self.begin_text_item_edit(item, select_all=False)
        return item

    def current_text_size_pt(self) -> int:
        if hasattr(self.ui, "spinBoxTextSize"):
            return max(1, int(self.ui.spinBoxTextSize.value()))
        return max(1, int(self.default_text_size_pt))

    @staticmethod
    def _default_text_box_width_pt(font_size_pt: int) -> float:
        return max(80.0, float(font_size_pt) * 12.0)

    def begin_text_item_edit(
        self,
        item: QGraphicsTextItem,
        *,
        select_all: bool = True,
    ) -> None:
        if item.scene() is not self.scene:
            return
        font_size = int(item.data(FONT_SIZE_PT_ROLE) or self.current_text_size_pt())
        item.setTextWidth(max(self._default_text_box_width_pt(font_size), item.boundingRect().width() + font_size * 4.0))
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.scene.clearSelection()
        item.setSelected(True)
        item.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = item.textCursor()
        if select_all and item.toPlainText():
            cursor.select(QTextCursor.SelectionType.Document)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
        item.setTextCursor(cursor)

    def current_text_editor(self) -> Optional[QGraphicsTextItem]:
        item = self.scene.focusItem()
        if isinstance(item, QGraphicsTextItem) and item.data(ITEM_KIND_ROLE) == KIND_TEXT:
            return item
        return None

    def is_text_item_being_edited(self) -> bool:
        item = self.current_text_editor()
        if item is None:
            return False
        return bool(item.textInteractionFlags() & Qt.TextInteractionFlag.TextEditorInteraction)

    def finish_current_text_edit(self, *, discard_empty: bool = True) -> None:
        item = self.current_text_editor()
        if item is not None:
            self.finish_text_item_edit(item, discard_empty=discard_empty)

    def finish_text_item_edit(
        self,
        item: QGraphicsTextItem,
        *,
        discard_empty: bool = True,
    ) -> None:
        if item.scene() is not self.scene:
            return
        text = item.toPlainText().strip()
        if discard_empty and not text:
            self._remove_text_item(item)
            return
        item.setPlainText(text)
        item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        item.clearFocus()
        item.setTextWidth(-1)
        item.setSelected(False)
        item.setData(TEXT_REGISTERED_ROLE, True)
        self._update_status_bar()

    def _remove_text_item(self, item: QGraphicsTextItem) -> None:
        aid = int(item.data(ANNOTATION_ID_ROLE) or 0)
        self.undo_stack = [entry for entry in self.undo_stack if entry[1] != aid]
        if item.scene() is self.scene:
            self.scene.removeItem(item)
        self._update_status_bar()

    def _configure_annotation_item(self, item: QGraphicsItem, kind: str, annotation_id: Optional[int] = None) -> None:
        if annotation_id is None:
            annotation_id = self._take_next_annotation_id()
        item.setData(ITEM_KIND_ROLE, kind)
        item.setData(ANNOTATION_ID_ROLE, int(annotation_id))
        flags = (
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        if kind == KIND_TEXT:
            flags |= QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
        item.setFlags(flags)
        item.setZValue(50)

    def _take_next_annotation_id(self) -> int:
        aid = self._next_annotation_id
        self._next_annotation_id += 1
        return aid

    # ------------------------------------------------------------------
    # Annotation lifecycle
    # ------------------------------------------------------------------

    def register_new_annotation(self, item: QGraphicsItem) -> None:
        aid = int(item.data(ANNOTATION_ID_ROLE) or 0)
        if aid <= 0:
            aid = self._take_next_annotation_id()
            item.setData(ANNOTATION_ID_ROLE, aid)
        if (self.current_page_index, aid) not in self.undo_stack:
            self.undo_stack.append((self.current_page_index, aid))
        if item.data(ITEM_KIND_ROLE) == KIND_TEXT:
            item.setData(TEXT_REGISTERED_ROLE, True)
        self._update_status_bar()

    def undo_last_draw(self) -> None:
        if not self.undo_stack:
            self.statusBar().showMessage("Nothing to undo.", 3000)
            return
        page_idx, aid = self.undo_stack.pop()
        if page_idx == self.current_page_index:
            item = self._find_scene_annotation(aid)
            if item is not None:
                self.scene.removeItem(item)
                self.statusBar().showMessage("Last annotation removed.", 3000)
            else:
                self.statusBar().showMessage("Annotation not found.", 3000)
        else:
            marks = self.annotations_by_page.get(page_idx, [])
            self.annotations_by_page[page_idx] = [m for m in marks if int(m.get("id", 0)) != aid]
            self.statusBar().showMessage("Annotation removed.", 3000)
        self._update_status_bar()

    def delete_selected_annotations(self) -> None:
        if self.is_text_item_being_edited():
            return
        selected = [
            item for item in self.scene.selectedItems()
            if item.data(ITEM_KIND_ROLE) not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)
        ]
        if not selected:
            return
        removed_ids = {int(item.data(ANNOTATION_ID_ROLE) or 0) for item in selected}
        for item in selected:
            self.scene.removeItem(item)
        self.undo_stack = [entry for entry in self.undo_stack if entry[1] not in removed_ids]
        self.statusBar().showMessage(f"Deleted {len(removed_ids)} annotation(s).", 3000)
        self._update_status_bar()

    def _find_scene_annotation(self, annotation_id: int) -> Optional[QGraphicsItem]:
        for item in self.scene.items():
            if int(item.data(ANNOTATION_ID_ROLE) or 0) == annotation_id:
                return item
        return None

    def is_too_small_annotation(self, item: QGraphicsItem) -> bool:
        kind = item.data(ITEM_KIND_ROLE)
        if kind == KIND_CIRCLE and isinstance(item, QGraphicsEllipseItem):
            r = item.mapRectToScene(item.rect()).normalized()
            return r.width() < 2.0 or r.height() < 2.0
        if kind == KIND_RECTANGLE and isinstance(item, QGraphicsRectItem):
            r = item.mapRectToScene(item.rect()).normalized()
            return r.width() < 2.0 or r.height() < 2.0
        if kind == KIND_ARROW and isinstance(item, ArrowGraphicsItem):
            s, e = item.start_scene(), item.end_scene()
            return math.hypot(e.x() - s.x(), e.y() - s.y()) < 2.0
        return False

    # ------------------------------------------------------------------
    # Annotation serialization
    # ------------------------------------------------------------------

    def collect_current_page_annotations(self) -> None:
        if self.document is None:
            return
        marks: List[Dict[str, Any]] = []
        overlay = sorted(
            (item for item in self.scene.items()
             if item.data(ITEM_KIND_ROLE) not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)),
            key=lambda i: int(i.data(ANNOTATION_ID_ROLE) or 0),
        )
        for item in overlay:
            kind = item.data(ITEM_KIND_ROLE)
            aid = int(item.data(ANNOTATION_ID_ROLE) or 0)
            if kind == KIND_CIRCLE and isinstance(item, QGraphicsEllipseItem):
                r = item.mapRectToScene(item.rect()).normalized()
                marks.append({"kind": KIND_CIRCLE, "id": aid, "rect": (r.left(), r.top(), r.right(), r.bottom()),
                              "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt)})
            elif kind == KIND_RECTANGLE and isinstance(item, QGraphicsRectItem):
                r = item.mapRectToScene(item.rect()).normalized()
                marks.append({"kind": KIND_RECTANGLE, "id": aid, "rect": (r.left(), r.top(), r.right(), r.bottom()),
                              "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt)})
            elif kind == KIND_ARROW and isinstance(item, ArrowGraphicsItem):
                s = item.start_scene(); e = item.end_scene()
                marks.append({"kind": KIND_ARROW, "id": aid, "start": (s.x(), s.y()), "end": (e.x(), e.y()),
                              "pen_width_pt": float(item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt)})
            elif kind == KIND_TEXT and isinstance(item, QGraphicsTextItem):
                text = item.toPlainText().strip()
                if not text:
                    continue
                pos = item.scenePos()
                marks.append({"kind": KIND_TEXT, "id": aid, "pos": (pos.x(), pos.y()), "text": text,
                              "font_size_pt": int(item.data(FONT_SIZE_PT_ROLE) or self.default_text_size_pt)})
        self.annotations_by_page[self.current_page_index] = marks

    def _restore_page_annotations(self) -> None:
        max_id = self._next_annotation_id - 1
        for mark in self.annotations_by_page.get(self.current_page_index, []):
            kind = mark.get("kind")
            aid = int(mark.get("id", 0))
            max_id = max(max_id, aid)
            if kind == KIND_CIRCLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_ellipse_item(QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))), aid)
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_RECTANGLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_rectangle_item(QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))), aid)
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_ARROW:
                sx, sy = mark["start"]; ex, ey = mark["end"]
                item = self.create_arrow_item(QPointF(float(sx), float(sy)), QPointF(float(ex), float(ey)), aid)
                item.setData(PEN_WIDTH_PT_ROLE, float(mark.get("pen_width_pt", self.annotation_width_pt)))
                self.scene.addItem(item)
            elif kind == KIND_TEXT:
                x, y = mark["pos"]
                font_size = int(mark.get("font_size_pt", self.default_text_size_pt))
                item = EditableTextGraphicsItem(self, str(mark.get("text", "")))
                font = QFont("Arial")
                font.setPointSizeF(max(1.0, float(font_size)))
                item.setFont(font)
                item.setDefaultTextColor(self.annotation_qcolor)
                item.setTextWidth(-1)
                item.setPos(QPointF(float(x), float(y)))
                self._configure_annotation_item(item, KIND_TEXT, aid)
                item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
                item.setData(FONT_SIZE_PT_ROLE, font_size)
                item.setData(TEXT_REGISTERED_ROLE, True)
                self.scene.addItem(item)
        self._next_annotation_id = max(self._next_annotation_id, max_id + 1)

    # ------------------------------------------------------------------
    # Save annotations into PDF
    # ------------------------------------------------------------------

    def _write_annotations_to_pdf(self, output_doc: Any) -> None:
        for page_idx, marks in self.annotations_by_page.items():
            if not (0 <= page_idx < output_doc.page_count):
                continue
            page = output_doc[page_idx]
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
        corners = [QPointF(float(x0), float(y0)), QPointF(float(x1), float(y0)),
                   QPointF(float(x1), float(y1)), QPointF(float(x0), float(y1))]
        pts = [self.scene_point_to_pdf_point(page, c) for c in corners]
        xs = [p.x for p in pts]; ys = [p.y for p in pts]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    def _write_ellipse(self, page: Any, mark: Dict[str, Any]) -> None:
        page.draw_oval(self._mark_rect_to_pdf_rect(page, mark), color=self.annotation_rgb,
                       width=float(mark.get("pen_width_pt", self.annotation_width_pt)), overlay=True)

    def _write_rectangle(self, page: Any, mark: Dict[str, Any]) -> None:
        page.draw_rect(self._mark_rect_to_pdf_rect(page, mark), color=self.annotation_rgb,
                       width=float(mark.get("pen_width_pt", self.annotation_width_pt)), overlay=True)

    def _write_arrow(self, page: Any, mark: Dict[str, Any]) -> None:
        sx, sy = mark["start"]; ex, ey = mark["end"]
        start = self.scene_point_to_pdf_point(page, QPointF(float(sx), float(sy)))
        end = self.scene_point_to_pdf_point(page, QPointF(float(ex), float(ey)))
        width = float(mark.get("pen_width_pt", self.annotation_width_pt))
        page.draw_line(start, end, color=self.annotation_rgb, width=width, overlay=True)
        dx = end.x - start.x; dy = end.y - start.y
        length = math.hypot(dx, dy)
        if length <= 0.01:
            return
        angle = math.atan2(dy, dx)
        arrow_angle = math.radians(28.0)
        head_len = max(8.0, width * 7.0)
        left = fitz.Point(end.x - head_len * math.cos(angle - arrow_angle),
                          end.y - head_len * math.sin(angle - arrow_angle))
        right = fitz.Point(end.x - head_len * math.cos(angle + arrow_angle),
                           end.y - head_len * math.sin(angle + arrow_angle))
        page.draw_line(end, left, color=self.annotation_rgb, width=width, overlay=True)
        page.draw_line(end, right, color=self.annotation_rgb, width=width, overlay=True)

    def _write_text(self, page: Any, mark: Dict[str, Any]) -> None:
        text = str(mark.get("text", "")).strip()
        if not text:
            return
        x, y = mark["pos"]
        font_size = int(mark.get("font_size_pt", self.default_text_size_pt))
        line_height = font_size * 1.20
        for i, line in enumerate(text.splitlines()):
            if not line:
                continue
            baseline = QPointF(float(x), float(y) + font_size + i * line_height)
            pdf_pt = self.scene_point_to_pdf_point(page, baseline)
            page.insert_text(pdf_pt, line, fontsize=font_size, fontname="helv",
                             color=self.annotation_rgb, overlay=True)

    def scene_point_to_pdf_point(self, page: Any, point: QPointF) -> Any:
        p = fitz.Point(float(point.x()), float(point.y()))
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
        if rotation:
            try:
                p = p * page.derotation_matrix
            except Exception:
                pass
        return p

    # ------------------------------------------------------------------
    # Geometry and status
    # ------------------------------------------------------------------

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
        tool = self.current_tool.title() if self.current_tool else "Select"
        mode = self._fit_mode.replace("_", " ").title()
        render_mode = "Screen" if self.screen_matched_rendering else "DPI"
        parts = [
            f"Page {self.current_page_index + 1}/{self.document.page_count}",
            self._paper_size_text(page),
            f"Zoom {self.current_zoom * 100:.0f}%",
            f"Render {self.current_render_dpi:.0f} DPI {render_mode}",
            f"ThinLine {self.graphics_min_line_width_px:.1f}px",
            f"View {mode}",
            f"Tool {tool}",
        ]
        self.paper_size_label.setText("  |  ".join(parts))

    def _paper_size_text(self, page: Any) -> str:
        w_pt = float(page.rect.width); h_pt = float(page.rect.height)
        w_mm = w_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        h_mm = h_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        name = self._match_paper_name(w_mm, h_mm)
        orient = "Landscape" if w_mm > h_mm else "Portrait"
        w_in = w_mm / MM_PER_INCH; h_in = h_mm / MM_PER_INCH
        return f"{name} {orient} ({w_mm:.1f} x {h_mm:.1f} mm / {w_in:.2f} x {h_in:.2f} in)"

    @staticmethod
    def _match_paper_name(width_mm: float, height_mm: float) -> str:
        actual = sorted((width_mm, height_mm))
        best_name, best_err = "Custom", float("inf")
        for name, (w, h) in PAPER_SIZES_MM.items():
            expected = sorted((w, h))
            err = abs(actual[0] - expected[0]) + abs(actual[1] - expected[1])
            if err < best_err:
                best_name, best_err = name, err
        return best_name if best_err <= PAPER_MATCH_TOLERANCE_MM * 2 else "Custom"

    @staticmethod
    def _qcolor_to_pymupdf_rgb(color: QColor) -> Tuple[float, float, float]:
        return (color.redF(), color.greenF(), color.blueF())


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    if len(sys.argv) >= 3:
        input_pdf = sys.argv[1]
        output_path = sys.argv[2]
    else:
        QMessageBox.information(None, "Usage", "python pdf_editor_controller.py input.pdf output_folder_or_output.pdf")
        return 1
    editor = PdfCorrectionEditor(input_pdf, output_path)
    editor.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
