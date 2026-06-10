"""
PDF correction editor built on the ui_pdfEditor.py UI module.

Install:
    pip install PySide6 PyMuPDF

Run standalone:
    python pdf_editor_controller.py input.pdf output_folder_or_output.pdf

Embed in your app:
    editor = PdfCorrectionEditor(input_path="drawing.pdf", output_path="out/")
    editor.show()

Key design notes
----------------
* Rendering pipeline
  - The scene coordinate system uses PDF points (1 pt = 1/72 inch) directly.
    No artificial scaling is applied; zoom is handled by the view transform only.
  - Tiles are rendered with PyMuPDF at a DPI chosen so that one rendered pixel
    maps to exactly one logical screen pixel at the current zoom level.
    This matches the Acrobat-like rendering that keeps A0/A1 linework sharp.
  - DPI is capped at ``maximum_render_dpi`` and floored at ``minimum_render_dpi``
    so that very small zoom levels still look acceptable.
  - On HiDPI / Retina screens the device pixel ratio is read from QScreen and
    factored into the DPI calculation so tiles are never blurry.
  - Tiles are cached in an LRU cache bounded by ``cache_limit_mb``.

* Large sheets (A3 and above)
  - The DPI formula is based on the *physical screen size* of the visible page
    area (viewport pixels / screen DPI → inches) so large sheets at fit-to-page
    zoom produce the same apparent sharpness as A4.  The old code used scene
    units directly, which caused large sheets to render at a fraction of the
    required DPI.
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
except ImportError:  # pragma: no cover – older PyMuPDF imports as fitz
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

from ui_pdfEditor import Ui_MainWindow


# ---------------------------------------------------------------------------
# QGraphicsItem custom data-slot roles
# ---------------------------------------------------------------------------
ITEM_KIND_ROLE = 1001
ANNOTATION_ID_ROLE = 1002
FONT_SIZE_PT_ROLE = 1003
PEN_WIDTH_PT_ROLE = 1004

# Annotation-kind constants
KIND_PAGE_BACKGROUND = "page_background"
KIND_PAGE_TILE = "page_tile"
KIND_CIRCLE = "circle"        # drawn as ellipse; button label kept for UI compat
KIND_RECTANGLE = "rectangle"
KIND_ARROW = "arrow"
KIND_TEXT = "text"

# Unit conversions
PDF_POINTS_PER_INCH: float = 72.0
MM_PER_INCH: float = 25.4
PAPER_MATCH_TOLERANCE_MM: float = 3.0

# ISO / ANSI / ARCH paper sizes in mm (portrait w × h)
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


# ---------------------------------------------------------------------------
# ArrowGraphicsItem
# ---------------------------------------------------------------------------

class ArrowGraphicsItem(QGraphicsPathItem):
    """Selectable, movable arrow rendered as a QPainterPath in scene (pt) coords."""

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

    # ------------------------------------------------------------------
    def set_points(self, start: QPointF, end: QPointF) -> None:
        self.prepareGeometryChange()
        self._start = QPointF(start)
        self._end = QPointF(end)
        self._rebuild_path()

    def start_scene(self) -> QPointF:
        return self.mapToScene(self._start)

    def end_scene(self) -> QPointF:
        return self.mapToScene(self._end)

    # ------------------------------------------------------------------
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
            # Head length: never more than 45 % of shaft, never less than 6 pt
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


# ---------------------------------------------------------------------------
# TextAnnotationItem
# ---------------------------------------------------------------------------

class TextAnnotationItem(QGraphicsTextItem):
    """Inline editable text box placed directly at the mouse click point."""

    def __init__(
        self,
        text: str = "",
        editor: Optional["PdfCorrectionEditor"] = None,
        parent: Optional[QGraphicsItem] = None,
    ) -> None:
        super().__init__(text, parent)
        self.editor = editor
        self._editing = False
        self._finishing = False
        self._minimum_box = QRectF(0.0, 0.0, 100.0, 24.0)

    def set_minimum_box_size(self, width_pt: float, height_pt: float) -> None:
        self.prepareGeometryChange()
        self._minimum_box = QRectF(
            0.0,
            0.0,
            max(8.0, float(width_pt)),
            max(8.0, float(height_pt)),
        )

    def boundingRect(self) -> QRectF:  # type: ignore[override]
        base = super().boundingRect()
        if self._editing or not self.toPlainText().strip():
            return base.united(self._minimum_box)
        return base

    def start_editing(self) -> None:
        self._editing = True
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextEditorInteraction)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, True)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.update()

    def finish_editing(self, *, remove_if_empty: bool = True) -> None:
        if self._finishing:
            return
        self._finishing = True
        self._editing = False

        if remove_if_empty and not self.toPlainText().strip():
            if self.editor is not None:
                self.editor.remove_annotation_item(self, remove_from_undo=True)
            elif self.scene() is not None:
                self.scene().removeItem(self)
            self._finishing = False
            return

        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
        self.update()
        if self.editor is not None:
            self.editor._update_status_bar()
        self._finishing = False

    def focusOutEvent(self, event) -> None:  # type: ignore[override]
        super().focusOutEvent(event)
        self.finish_editing(remove_if_empty=True)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() == Qt.Key.Key_Escape:
            self.finish_editing(remove_if_empty=True)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers()
            if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                self.clearFocus()
                self.finish_editing(remove_if_empty=True)
                event.accept()
                return
        super().keyPressEvent(event)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # type: ignore[override]
        super().paint(painter, option, widget)
        if self._editing or not self.toPlainText().strip():
            painter.save()
            color = self.defaultTextColor()
            if not color.isValid():
                color = QColor("#ff0000")
            pen = QPen(color, 0.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            painter.drawRect(self.boundingRect().adjusted(0.5, 0.5, -0.5, -0.5))
            painter.restore()


# ---------------------------------------------------------------------------
# PdfGraphicsView
# ---------------------------------------------------------------------------

class PdfGraphicsView(QGraphicsView):
    """
    Custom QGraphicsView that handles:
      - Mouse-wheel zoom anchored at the pointer
      - Middle-button pan
      - Drawing new annotation shapes by drag
      - Keyboard shortcuts (Undo, Delete, Escape, PageUp/Down)
    """

    def __init__(
        self,
        editor: "PdfCorrectionEditor",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.editor = editor
        self._drawing_start: Optional[QPointF] = None
        self._temp_item: Optional[QGraphicsItem] = None
        self._panning = False
        self._pan_start = QPoint()
        self._pan_h_value = 0
        self._pan_v_value = 0

        hints = (
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )
        # Screen-matched PDF tiles should not be smoothed by Qt; otherwise
        # thin CAD/CAM/engineering lines become pale after scaling.
        if not getattr(editor, "screen_matched_rendering", True):
            hints |= QPainter.RenderHint.SmoothPixmapTransform
        self.setRenderHints(hints)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setBackgroundBrush(QBrush(QColor("#404040")))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setMouseTracking(True)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------
    # Wheel → zoom
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Mouse press / move / release
    # ------------------------------------------------------------------

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

            tool = self.editor.current_tool
            if tool == KIND_CIRCLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_ellipse_item(
                    QRectF(scene_pos, scene_pos)
                )
                self.scene().addItem(self._temp_item)
                event.accept()
                return

            if tool == KIND_RECTANGLE:
                self._drawing_start = scene_pos
                self._temp_item = self.editor.create_rectangle_item(
                    QRectF(scene_pos, scene_pos)
                )
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
            scene_pos = self.editor.clamp_to_page(
                self.mapToScene(event.position().toPoint())
            )
            force_square = bool(
                QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier
            )
            tool = self.editor.current_tool

            if tool == KIND_CIRCLE and isinstance(self._temp_item, QGraphicsEllipseItem):
                self._temp_item.setRect(
                    self._drag_rect(self._drawing_start, scene_pos, force_square)
                )
                event.accept()
                return

            if tool == KIND_RECTANGLE and isinstance(self._temp_item, QGraphicsRectItem):
                self._temp_item.setRect(
                    self._drag_rect(self._drawing_start, scene_pos, force_square)
                )
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

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Pan helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# PdfCorrectionEditor – main window
# ---------------------------------------------------------------------------

class PdfCorrectionEditor(QMainWindow):
    """
    Main window of the PDF correction editor.

    Parameters
    ----------
    input_path:
        PDF file to open.
    output_path:
        Destination folder *or* full output PDF path.
        When a folder is given the output file is ``<stem>_annotated.pdf``
        inside that folder.
    minimum_render_dpi:
        Floor for tile render DPI. In the default Acrobat-like mode this is ignored, so old high-DPI settings cannot soften A0/A1 linework.
    maximum_render_dpi:
        Ceiling for tile render DPI.  600 is safe for up to ~30× zoom.
    render_quality:
        Oversample factor used only when screen_matched_rendering=False.
        In the default Acrobat-like mode the viewer ignores this value so A0/A1
        sheets are not over-rendered and softened by downscaling.
    cache_limit_mb:
        Maximum tile-cache memory in MB.  512 MB is comfortable for A0 work.
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
        cache_limit_mb: int = 512,
        max_visible_render_pixels: int = 200_000_000,
        tile_pixel_size: int = 2048,
        min_zoom: float = 0.01,
        max_zoom: float = 32.0,
        annotation_width_pt: float = 1.5,
        annotation_color: str = "#ff0000",
        default_text_size_pt: int = 15,
        screen_matched_rendering: bool = True,
        graphics_min_line_width_px: float = 1.0,
        auto_text_size_by_paper: bool = True,
        base_text_size_a4_pt: float = 15.0,
        min_auto_text_size_pt: int = 8,
        max_auto_text_size_pt: int = 150,
        auto_text_size: Optional[bool] = None,
        a4_text_size_pt: Optional[float] = None,
    ) -> None:
        if QApplication.instance() is None:
            raise RuntimeError(
                "A QApplication must exist before creating PdfCorrectionEditor."
            )
        if parent is not None and not isinstance(parent, QWidget):
            raise TypeError(
                "parent must be None or a QWidget/QMainWindow; "
                "do not pass Ui_MainWindow as parent."
            )

        super().__init__(parent)
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        # ── Paths ──────────────────────────────────────────────────────────
        self.input_path = Path(input_path).expanduser().resolve()
        self.output_path = self._resolve_output_path(output_path)

        # ── Rendering parameters ───────────────────────────────────────────
        self.minimum_render_dpi = float(minimum_render_dpi)
        self.maximum_render_dpi = float(maximum_render_dpi)
        self.render_quality = float(render_quality)
        self.screen_matched_rendering = bool(screen_matched_rendering)
        self.graphics_min_line_width_px = float(graphics_min_line_width_px)
        self.cache_limit_bytes = max(16, int(cache_limit_mb)) * 1024 * 1024
        self.max_visible_render_pixels = max(1_000_000, int(max_visible_render_pixels))
        self.tile_pixel_size = max(256, int(tile_pixel_size))
        self.min_zoom = float(min_zoom)
        self.max_zoom = float(max_zoom)

        # ── Runtime state ──────────────────────────────────────────────────
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
        self._apply_mupdf_graphics_options()

        self.document: Optional[Any] = None
        self.current_page_index: int = 0
        self.current_tool: Optional[str] = None
        self.page_scene_rect = QRectF()
        self._fit_mode: bool = True
        self._next_annotation_id: int = 1
        self._updating_page_spinbox: bool = False

        self.annotations_by_page: Dict[int, List[Dict[str, Any]]] = {}
        self.undo_stack: List[Tuple[int, int]] = []  # (page_index, annotation_id)

        # ── Scene / view ───────────────────────────────────────────────────
        self.scene = QGraphicsScene(self)
        self.view = self._replace_ui_graphics_view()
        self.view.setScene(self.scene)

        # ── Tile machinery ─────────────────────────────────────────────────
        self._page_background_item: Optional[QGraphicsRectItem] = None
        self._tile_items: List[QGraphicsPixmapItem] = []
        self._tile_cache: OrderedDict[
            Tuple[Any, ...], Tuple[QPixmap, int]
        ] = OrderedDict()
        self._tile_cache_bytes: int = 0

        self._tile_timer = QTimer(self)
        self._tile_timer.setSingleShot(True)
        self._tile_timer.timeout.connect(self.update_visible_tiles)

        # ── Status bar ─────────────────────────────────────────────────────
        self.paper_size_label = QLabel(self)
        self.paper_size_label.setMinimumWidth(680)
        self.paper_size_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self.paper_size_label, 1)

        # ── Finish setup ───────────────────────────────────────────────────
        self._configure_ui()
        self._connect_signals()
        self._create_shortcuts()
        self.load_pdf(self.input_path)

    # ======================================================================
    # UI setup
    # ======================================================================

    def _replace_ui_graphics_view(self) -> PdfGraphicsView:
        """Swap the placeholder QGraphicsView for our custom PdfGraphicsView."""
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
        self.setWindowTitle(f"PDF Correction Editor — {self.input_path.name}")

        # Dark background for the viewer panel
        if hasattr(self.ui, "viewerWindow"):
            self.ui.viewerWindow.setStyleSheet("background: #404040;")
        self.ui.graphicsView.setStyleSheet(
            "QGraphicsView { background: #404040; border: 0px; }"
        )
        self.scene.setBackgroundBrush(QBrush(QColor("#404040")))

        # Tool buttons are already checkable from the UI file,
        # but guard for old UI files that might not have them.
        for name in ("btnCircle", "btnRectangle", "btnArrow", "btnText"):
            btn = getattr(self.ui, name, None)
            if btn is not None:
                btn.setCheckable(True)

        # Text-size spinbox
        if hasattr(self.ui, "spinBoxTextSize"):
            sb = self.ui.spinBoxTextSize
            sb.setRange(1, 300)
            if sb.value() <= 0:
                sb.setValue(self.default_text_size_pt)
            sb.setSuffix(" pt")
            sb.setToolTip(
                "Font size for new text annotations. Auto-set from paper size; "
                "A4 uses 15 pt as the reference."
            )

        # Page spinbox
        if hasattr(self.ui, "spinBoxPageNo"):
            self.ui.spinBoxPageNo.setRange(1, 1)
            self.ui.spinBoxPageNo.setToolTip("Current page number")

        # Correction button tooltip
        if hasattr(self.ui, "btnCorrection"):
            self.ui.btnCorrection.setToolTip(
                "Save annotated PDF to the configured output path"
            )

        # Scroll-bar movements trigger a (debounced) tile refresh
        self.view.horizontalScrollBar().valueChanged.connect(
            lambda _: self.schedule_tile_update(30)
        )
        self.view.verticalScrollBar().valueChanged.connect(
            lambda _: self.schedule_tile_update(30)
        )
        self.apply_cursor_for_tool()

    def _connect_signals(self) -> None:
        def _tool_toggle(kind: str):
            return lambda: self.set_tool(kind if self.current_tool != kind else None)

        if hasattr(self.ui, "btnCircle"):
            self.ui.btnCircle.clicked.connect(_tool_toggle(KIND_CIRCLE))
        if hasattr(self.ui, "btnRectangle"):
            self.ui.btnRectangle.clicked.connect(_tool_toggle(KIND_RECTANGLE))
        if hasattr(self.ui, "btnArrow"):
            self.ui.btnArrow.clicked.connect(_tool_toggle(KIND_ARROW))
        if hasattr(self.ui, "btnText"):
            self.ui.btnText.clicked.connect(_tool_toggle(KIND_TEXT))
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
            (QKeySequence.StandardKey.Undo, self.undo_last_draw),
            (QKeySequence("Delete"), self.delete_selected_annotations),
            (QKeySequence.StandardKey.ZoomIn, lambda: self.zoom_at_view_center(1.15)),
            (
                QKeySequence.StandardKey.ZoomOut,
                lambda: self.zoom_at_view_center(1.0 / 1.15),
            ),
            (QKeySequence("Ctrl+0"), self.fit_page_to_view),
        ]
        for key, slot in shortcuts:
            action = QAction(self)
            action.setShortcut(key)
            action.triggered.connect(slot)
            # ShortcutContext = Widget — ensures no double-fire with the view's
            # own keyPressEvent handlers.
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)

    # ======================================================================
    # Document loading / saving
    # ======================================================================

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

        # Prevent overwriting the source file
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
            QMessageBox.critical(
                self, "Save failed", f"Could not save annotated PDF:\n{exc}"
            )
            return None

        self.statusBar().showMessage(f"Saved: {target}", 8000)
        self.correction_saved.emit(str(target))
        QMessageBox.information(
            self, "Correction saved", f"Annotated PDF saved to:\n{target}"
        )
        return target

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._tile_timer.stop()
        self._clear_tile_cache()
        if self.document is not None:
            self.document.close()
            self.document = None
        super().closeEvent(event)

    # ======================================================================
    # Rendering
    # ======================================================================

    def render_current_page(self, *, fit_after_render: bool = False) -> None:
        if self.document is None:
            self.scene.clear()
            self.page_scene_rect = QRectF()
            self._update_status_bar()
            return

        self.scene.clear()
        self._tile_items.clear()

        page = self.document[self.current_page_index]
        # Scene coordinates = PDF points (1 pt = 1/72 inch).
        # No pre-scaling: zoom is applied entirely via the view transform.
        self.page_scene_rect = QRectF(
            0.0, 0.0, float(page.rect.width), float(page.rect.height)
        )
        self.scene.setSceneRect(self.page_scene_rect)
        self._apply_auto_text_size_for_current_page()

        # White page background (appears behind tiles)
        bg = QGraphicsRectItem(self.page_scene_rect)
        bg.setData(ITEM_KIND_ROLE, KIND_PAGE_BACKGROUND)
        bg.setPen(QPen(QColor("#bdbdbd"), 0.0))
        bg.setBrush(QBrush(Qt.GlobalColor.white))
        bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        bg.setZValue(-200)
        self.scene.addItem(bg)
        self._page_background_item = bg

        self._restore_page_annotations()
        self._update_page_spinbox_value()
        self._update_status_bar()

        if fit_after_render:
            self._fit_mode = True
            QTimer.singleShot(0, self.fit_page_to_view)
        else:
            self.schedule_tile_update(0)

    def schedule_tile_update(self, delay_ms: int = 30) -> None:
        if self.document is None or self.page_scene_rect.isNull():
            return
        self._tile_timer.start(max(0, int(delay_ms)))

    def update_visible_tiles(self) -> None:
        """Render and display only the tiles covering the visible viewport area."""
        if self.document is None or self.page_scene_rect.isNull():
            return

        page = self.document[self.current_page_index]
        visible_rect = self._visible_page_rect()
        if visible_rect.isNull() or visible_rect.width() <= 0 or visible_rect.height() <= 0:
            return

        render_dpi = self._choose_render_dpi()
        self.current_render_dpi = render_dpi
        render_scale = render_dpi / PDF_POINTS_PER_INCH  # pixels per PDF point
        # Each tile covers tile_pixel_size rendered pixels → scene-unit size
        tile_scene_size = max(64.0, self.tile_pixel_size / render_scale)

        # Remove stale tiles (keep background + annotation items)
        for item in self._tile_items:
            if item.scene() is self.scene:
                self.scene.removeItem(item)
        self._tile_items.clear()

        # Iterate over all tile positions that overlap the visible rect
        x0 = math.floor(visible_rect.left() / tile_scene_size) * tile_scene_size
        y0 = math.floor(visible_rect.top() / tile_scene_size) * tile_scene_size
        x1 = math.ceil(visible_rect.right() / tile_scene_size) * tile_scene_size
        y1 = math.ceil(visible_rect.bottom() / tile_scene_size) * tile_scene_size

        x = x0
        while x < x1:
            y = y0
            while y < y1:
                clip = QRectF(x, y, tile_scene_size, tile_scene_size).intersected(
                    self.page_scene_rect
                )
                if clip.width() > 0.5 and clip.height() > 0.5:
                    pixmap = self._render_tile_pixmap(page, clip, render_dpi)
                    tile_item = QGraphicsPixmapItem(pixmap)
                    tile_item.setData(ITEM_KIND_ROLE, KIND_PAGE_TILE)
                    tile_item.setFlag(
                        QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False
                    )
                    tile_item.setTransformationMode(
                        Qt.TransformationMode.FastTransformation
                        if self.screen_matched_rendering
                        else Qt.TransformationMode.SmoothTransformation
                    )
                    tile_item.setZValue(-100)
                    tile_item.setPos(clip.left(), clip.top())
                    # Scale the pixmap back to scene (PDF-point) units
                    tile_item.setScale(1.0 / render_scale)
                    self.scene.addItem(tile_item)
                    self._tile_items.append(tile_item)
                y += tile_scene_size
            x += tile_scene_size

        self._update_status_bar()

    # ------------------------------------------------------------------
    # Visible-rect helpers
    # ------------------------------------------------------------------

    def _visible_page_rect(self) -> QRectF:
        """
        Visible scene rect intersected with the page rect, with generous
        prefetch padding so that slow panning does not expose gray areas.
        """
        vp_rect = self.view.viewport().rect()
        visible = self.view.mapToScene(vp_rect).boundingRect().intersected(
            self.page_scene_rect
        )
        if visible.isNull():
            return visible
        # 25 % prefetch border on each side
        pad_x = visible.width() * 0.25
        pad_y = visible.height() * 0.25
        visible.adjust(-pad_x, -pad_y, pad_x, pad_y)
        return visible.intersected(self.page_scene_rect)

    def _choose_render_dpi(self) -> float:
        """
        Compute the tile render DPI so that one rendered pixel occupies
        exactly one logical screen pixel at the current zoom, then multiply
        by ``render_quality`` for sharpness.

        The critical fix for large sheets (A3+):
        ----------------------------------------
        DPI must be derived from *physical screen pixels per inch*, not from
        scene points.  At fit-to-page zoom an A0 sheet may be mapped to only
        ~6 pt/pixel in scene space, but the viewport still has the same
        physical pixel density.  Using ``current_zoom * PDF_POINTS_PER_INCH``
        under-estimates the required DPI for large sheets because the zoom is
        small; instead we measure how many viewport pixels the visible page
        occupies and compute DPI from there.
        """
        vp_rect = self.view.viewport().rect()
        visible_scene = self.view.mapToScene(vp_rect).boundingRect().intersected(
            self.page_scene_rect
        )

        # Pixels covering the visible page area in viewport coordinates
        visible_vp = self.view.mapFromScene(visible_scene).boundingRect()
        vp_px_w = max(1.0, float(visible_vp.width()))
        vp_px_h = max(1.0, float(visible_vp.height()))

        # Physical size of the visible page area in inches
        page_in_w = max(0.001, visible_scene.width() / PDF_POINTS_PER_INCH)
        page_in_h = max(0.001, visible_scene.height() / PDF_POINTS_PER_INCH)

        # DPI needed to map one page-inch to the correct number of screen pixels
        dpi_x = vp_px_w / page_in_w
        dpi_y = vp_px_h / page_in_h
        screen_dpi = max(dpi_x, dpi_y)

        # Acrobat-like rendering mode:
        #   Render at the actual screen pixel density and let MuPDF's
        #   thin-line option keep CAD/engineering linework visible.  Do not
        #   oversample and downscale in this mode; that is what made A0 sheets
        #   look pale/blurred compared with Acrobat.  We intentionally ignore
        #   high minimum_render_dpi / render_quality values here so older call
        #   sites cannot accidentally switch the viewer back to the soft
        #   downscaled pipeline.
        dpr = self._device_pixel_ratio()
        if self.screen_matched_rendering:
            target = screen_dpi * dpr
            dpi_floor = 1.0
        else:
            target = screen_dpi * self.render_quality * dpr
            dpi_floor = max(1.0, self.minimum_render_dpi)

        # Pixel-count guard: protect only the visible tile region, not the whole
        # sheet. A0 pages can remain crisp at fit-to-page without forcing a huge
        # full-page bitmap allocation.
        visible_in = (
            max(0.001, visible_scene.width() / PDF_POINTS_PER_INCH),
            max(0.001, visible_scene.height() / PDF_POINTS_PER_INCH),
        )
        pixels_at_target = visible_in[0] * visible_in[1] * target * target
        if pixels_at_target > self.max_visible_render_pixels:
            target *= math.sqrt(self.max_visible_render_pixels / pixels_at_target)
            target = max(dpi_floor, target)

        return max(dpi_floor, min(self.maximum_render_dpi, target))

    def _device_pixel_ratio(self) -> float:
        """Return the device pixel ratio of the screen that hosts the window."""
        screen = self.screen()
        if screen is not None:
            return float(screen.devicePixelRatio())
        app = QApplication.instance()
        if app is not None:
            screens = app.screens()
            if screens:
                return float(screens[0].devicePixelRatio())
        return 1.0

    # ------------------------------------------------------------------
    # Tile rendering & cache
    # ------------------------------------------------------------------

    def _render_tile_pixmap(
        self, page: Any, rect: QRectF, render_dpi: float
    ) -> QPixmap:
        """
        Render a single tile, returning a cached QPixmap when available.
        The cache key uses rounded coordinates to maximise hit rate while
        staying numerically stable.
        """
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
            self._tile_cache.move_to_end(key)
            return cached[0]

        render_scale = render_dpi / PDF_POINTS_PER_INCH
        matrix = fitz.Matrix(render_scale, render_scale)
        clip = fitz.Rect(
            float(rect.left()),
            float(rect.top()),
            float(rect.right()),
            float(rect.bottom()),
        )
        # annots=False: existing PDF annotations are not rendered into tiles
        # because we draw them ourselves as QGraphicsItems.
        pix = page.get_pixmap(
            matrix=matrix, clip=clip, alpha=False, colorspace=fitz.csRGB, annots=False
        )
        image = QImage(
            pix.samples, pix.width, pix.height, pix.stride,
            QImage.Format.Format_RGB888,
        ).copy()
        pixmap = QPixmap.fromImage(image)

        byte_count = max(1, pixmap.width() * pixmap.height() * 4)
        self._tile_cache[key] = (pixmap, byte_count)
        self._tile_cache_bytes += byte_count
        self._evict_tile_cache()
        return pixmap

    def _evict_tile_cache(self) -> None:
        """Evict oldest entries until the cache is within the byte limit."""
        while self._tile_cache_bytes > self.cache_limit_bytes and self._tile_cache:
            _, (_, byte_count) = self._tile_cache.popitem(last=False)
            self._tile_cache_bytes -= byte_count

    def _clear_tile_cache(self) -> None:
        self._tile_cache.clear()
        self._tile_cache_bytes = 0

    # ------------------------------------------------------------------
    # Window resize
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._fit_mode and not self.page_scene_rect.isNull():
            QTimer.singleShot(0, self.fit_page_to_view)
        else:
            self.schedule_tile_update(50)

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

    # ======================================================================
    # Zoom & pan
    # ======================================================================

    def fit_page_to_view(self) -> None:
        if self.page_scene_rect.isNull():
            return
        vp = self.view.viewport().size()
        margin = 24
        avail_w = max(1, vp.width() - 2 * margin)
        avail_h = max(1, vp.height() - 2 * margin)
        zoom_x = avail_w / self.page_scene_rect.width()
        zoom_y = avail_h / self.page_scene_rect.height()
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
        actual = new_zoom / self.current_zoom if self.current_zoom else 1.0
        if abs(actual - 1.0) < 1e-4:
            return
        self.view.scale(actual, actual)
        self.current_zoom = new_zoom
        self.schedule_tile_update(20)
        self._update_status_bar()

    # ======================================================================
    # Tool management
    # ======================================================================

    def set_tool(self, tool: Optional[str]) -> None:
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

    # ======================================================================
    # Annotation item factories
    # ======================================================================

    def _annotation_pen(self) -> QPen:
        pen = QPen(self.annotation_qcolor, self.annotation_width_pt)
        pen.setCosmetic(False)  # width in scene (pt) units, not pixels
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        return pen

    def create_ellipse_item(
        self,
        rect: QRectF,
        annotation_id: Optional[int] = None,
    ) -> QGraphicsEllipseItem:
        item = QGraphicsEllipseItem(rect.normalized())
        item.setPen(self._annotation_pen())
        item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        self._configure_annotation_item(item, KIND_CIRCLE, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    # Alias for backwards compatibility
    def create_circle_item(
        self,
        rect: QRectF,
        annotation_id: Optional[int] = None,
    ) -> QGraphicsEllipseItem:
        return self.create_ellipse_item(rect, annotation_id)

    def create_rectangle_item(
        self,
        rect: QRectF,
        annotation_id: Optional[int] = None,
    ) -> QGraphicsRectItem:
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
        # Arrow head length scales with page size so it is visible on large sheets.
        page_diag_pt = math.hypot(
            self.page_scene_rect.width(), self.page_scene_rect.height()
        )
        head_len = max(12.0, page_diag_pt * 0.008)  # ~0.8 % of page diagonal
        item = ArrowGraphicsItem(start, end, self._annotation_pen(), head_len)
        self._configure_annotation_item(item, KIND_ARROW, annotation_id)
        item.setData(PEN_WIDTH_PT_ROLE, self.annotation_width_pt)
        return item

    def add_text_at(
        self,
        scene_pos: QPointF,
        text: Optional[str] = None,
        *,
        start_editing: Optional[bool] = None,
    ) -> Optional[QGraphicsTextItem]:
        """Create a text annotation directly at the clicked PDF location.

        When ``text`` is omitted the item is inserted as an editable text box
        with keyboard focus, so the user can type immediately at the click
        point.  The old modal input dialog is intentionally not used.
        """
        interactive = text is None if start_editing is None else bool(start_editing)
        initial_text = "" if text is None else str(text)
        if not interactive and not initial_text.strip():
            return None

        font_size_pt = self.current_text_size_pt()
        item = TextAnnotationItem(initial_text, editor=self)
        font = QFont("Arial")
        # Use setPointSizeF so the font size is in PDF points (scene units),
        # matching the coordinate system exactly.
        font.setPointSizeF(max(1.0, float(font_size_pt)))
        item.setFont(font)
        item.setDefaultTextColor(self.annotation_qcolor)
        item.setTextWidth(-1)
        item.set_minimum_box_size(
            max(90.0, float(font_size_pt) * 6.0),
            max(20.0, float(font_size_pt) * 1.7),
        )
        item.setPos(self.clamp_to_page(scene_pos))
        self._configure_annotation_item(item, KIND_TEXT)
        item.setData(FONT_SIZE_PT_ROLE, font_size_pt)
        self.scene.addItem(item)
        self.register_new_annotation(item)

        if interactive:
            item.start_editing()
        else:
            item.finish_editing(remove_if_empty=False)
        return item

    def current_text_size_pt(self) -> int:
        if hasattr(self.ui, "spinBoxTextSize"):
            return max(1, int(self.ui.spinBoxTextSize.value()))
        return max(1, int(self.default_text_size_pt))

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
        aid = self._next_annotation_id
        self._next_annotation_id += 1
        return aid

    # ======================================================================
    # Annotation lifecycle
    # ======================================================================

    def register_new_annotation(self, item: QGraphicsItem) -> None:
        aid = int(item.data(ANNOTATION_ID_ROLE) or 0)
        if aid <= 0:
            aid = self._take_next_annotation_id()
            item.setData(ANNOTATION_ID_ROLE, aid)
        self.undo_stack.append((self.current_page_index, aid))
        self._update_status_bar()

    def remove_annotation_item(
        self, item: QGraphicsItem, *, remove_from_undo: bool = False
    ) -> None:
        aid = int(item.data(ANNOTATION_ID_ROLE) or 0)
        if item.scene() is self.scene:
            self.scene.removeItem(item)
        if remove_from_undo and aid > 0:
            self.undo_stack = [entry for entry in self.undo_stack if entry[1] != aid]
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
                self.statusBar().showMessage("Annotation not found in scene.", 3000)
        else:
            marks = self.annotations_by_page.get(page_idx, [])
            before = len(marks)
            self.annotations_by_page[page_idx] = [
                m for m in marks if int(m.get("id", 0)) != aid
            ]
            removed = len(self.annotations_by_page[page_idx]) < before
            self.statusBar().showMessage(
                "Annotation removed." if removed else "Could not find annotation.",
                3000,
            )
        self._update_status_bar()

    def delete_selected_annotations(self) -> None:
        selected = [
            item
            for item in self.scene.selectedItems()
            if item.data(ITEM_KIND_ROLE) not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)
        ]
        if not selected:
            return
        removed_ids = {int(item.data(ANNOTATION_ID_ROLE) or 0) for item in selected}
        for item in selected:
            self.scene.removeItem(item)
        self.undo_stack = [
            entry for entry in self.undo_stack if entry[1] not in removed_ids
        ]
        count = len(removed_ids)
        self.statusBar().showMessage(
            f"{count} annotation{'s' if count != 1 else ''} deleted.", 3000
        )
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

    # ======================================================================
    # Annotation persistence (in-memory serialisation per page)
    # ======================================================================

    def collect_current_page_annotations(self) -> None:
        if self.document is None:
            return
        marks: List[Dict[str, Any]] = []
        overlay = sorted(
            (
                item
                for item in self.scene.items()
                if item.data(ITEM_KIND_ROLE)
                not in (KIND_PAGE_BACKGROUND, KIND_PAGE_TILE)
            ),
            key=lambda i: int(i.data(ANNOTATION_ID_ROLE) or 0),
        )
        for item in overlay:
            kind = item.data(ITEM_KIND_ROLE)
            aid = int(item.data(ANNOTATION_ID_ROLE) or 0)

            if kind == KIND_CIRCLE and isinstance(item, QGraphicsEllipseItem):
                r = item.mapRectToScene(item.rect()).normalized()
                marks.append(
                    {
                        "kind": KIND_CIRCLE,
                        "id": aid,
                        "rect": (r.left(), r.top(), r.right(), r.bottom()),
                        "pen_width_pt": float(
                            item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt
                        ),
                    }
                )
            elif kind == KIND_RECTANGLE and isinstance(item, QGraphicsRectItem):
                r = item.mapRectToScene(item.rect()).normalized()
                marks.append(
                    {
                        "kind": KIND_RECTANGLE,
                        "id": aid,
                        "rect": (r.left(), r.top(), r.right(), r.bottom()),
                        "pen_width_pt": float(
                            item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt
                        ),
                    }
                )
            elif kind == KIND_ARROW and isinstance(item, ArrowGraphicsItem):
                s = item.start_scene()
                e = item.end_scene()
                marks.append(
                    {
                        "kind": KIND_ARROW,
                        "id": aid,
                        "start": (s.x(), s.y()),
                        "end": (e.x(), e.y()),
                        "pen_width_pt": float(
                            item.data(PEN_WIDTH_PT_ROLE) or self.annotation_width_pt
                        ),
                    }
                )
            elif kind == KIND_TEXT and isinstance(item, QGraphicsTextItem):
                plain_text = item.toPlainText().strip()
                if not plain_text:
                    continue
                pos = item.scenePos()
                marks.append(
                    {
                        "kind": KIND_TEXT,
                        "id": aid,
                        "pos": (pos.x(), pos.y()),
                        "text": plain_text,
                        "font_size_pt": int(
                            item.data(FONT_SIZE_PT_ROLE) or self.default_text_size_pt
                        ),
                    }
                )
        self.annotations_by_page[self.current_page_index] = marks

    def _restore_page_annotations(self) -> None:
        max_id = self._next_annotation_id - 1
        for mark in self.annotations_by_page.get(self.current_page_index, []):
            kind = mark.get("kind")
            aid = int(mark.get("id", 0))
            max_id = max(max_id, aid)

            if kind == KIND_CIRCLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_ellipse_item(
                    QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))),
                    annotation_id=aid,
                )
                item.setData(
                    PEN_WIDTH_PT_ROLE,
                    float(mark.get("pen_width_pt", self.annotation_width_pt)),
                )
                self.scene.addItem(item)

            elif kind == KIND_RECTANGLE:
                x0, y0, x1, y1 = mark["rect"]
                item = self.create_rectangle_item(
                    QRectF(QPointF(float(x0), float(y0)), QPointF(float(x1), float(y1))),
                    annotation_id=aid,
                )
                item.setData(
                    PEN_WIDTH_PT_ROLE,
                    float(mark.get("pen_width_pt", self.annotation_width_pt)),
                )
                self.scene.addItem(item)

            elif kind == KIND_ARROW:
                sx, sy = mark["start"]
                ex, ey = mark["end"]
                item = self.create_arrow_item(
                    QPointF(float(sx), float(sy)),
                    QPointF(float(ex), float(ey)),
                    annotation_id=aid,
                )
                item.setData(
                    PEN_WIDTH_PT_ROLE,
                    float(mark.get("pen_width_pt", self.annotation_width_pt)),
                )
                self.scene.addItem(item)

            elif kind == KIND_TEXT:
                x, y = mark["pos"]
                font_size = int(mark.get("font_size_pt", self.default_text_size_pt))
                item = TextAnnotationItem(str(mark.get("text", "")), editor=self)
                font = QFont("Arial")
                font.setPointSizeF(max(1.0, float(font_size)))
                item.setFont(font)
                item.setDefaultTextColor(self.annotation_qcolor)
                item.setTextWidth(-1)
                item.set_minimum_box_size(
                    max(90.0, float(font_size) * 6.0),
                    max(20.0, float(font_size) * 1.7),
                )
                item.setPos(QPointF(float(x), float(y)))
                self._configure_annotation_item(item, KIND_TEXT, annotation_id=aid)
                item.setData(FONT_SIZE_PT_ROLE, font_size)
                item.finish_editing(remove_if_empty=False)
                self.scene.addItem(item)

        self._next_annotation_id = max(self._next_annotation_id, max_id + 1)

    # ======================================================================
    # Writing annotations into the output PDF (PyMuPDF)
    # ======================================================================

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
        corners = [
            QPointF(float(x0), float(y0)),
            QPointF(float(x1), float(y0)),
            QPointF(float(x1), float(y1)),
            QPointF(float(x0), float(y1)),
        ]
        pts = [self.scene_point_to_pdf_point(page, c) for c in corners]
        xs = [p.x for p in pts]
        ys = [p.y for p in pts]
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
        left = fitz.Point(
            end.x - head_len * math.cos(angle - arrow_angle),
            end.y - head_len * math.sin(angle - arrow_angle),
        )
        right = fitz.Point(
            end.x - head_len * math.cos(angle + arrow_angle),
            end.y - head_len * math.sin(angle + arrow_angle),
        )
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
            # Baseline is below the top-left origin of the text block
            baseline = QPointF(float(x), float(y) + font_size + i * line_height)
            pdf_pt = self.scene_point_to_pdf_point(page, baseline)
            page.insert_text(
                pdf_pt,
                line,
                fontsize=font_size,
                fontname="helv",
                color=self.annotation_rgb,
                overlay=True,
            )

    def scene_point_to_pdf_point(self, page: Any, point: QPointF) -> Any:
        """
        Convert a scene coordinate (PDF points, top-left origin) to a
        PyMuPDF page point, accounting for page rotation.
        """
        p = fitz.Point(float(point.x()), float(point.y()))
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
        if rotation:
            try:
                p = p * page.derotation_matrix
            except Exception:
                pass
        return p

    # ======================================================================
    # Text-size automation / rendering options
    # ======================================================================

    def _apply_mupdf_graphics_options(self) -> None:
        """Optionally ask MuPDF to keep very thin vector lines visible during rendering."""
        width_px = max(0.0, float(self.graphics_min_line_width_px))
        tools = getattr(fitz, "TOOLS", None)
        setter = getattr(tools, "set_graphics_min_line_width", None) if tools is not None else None
        if setter is None:
            return
        try:
            setter(width_px)
        except Exception:
            # Older PyMuPDF builds may not support this; rendering still works.
            pass

    def _apply_auto_text_size_for_current_page(self) -> None:
        if not self.auto_text_size_by_paper:
            return
        if self.document is None or not hasattr(self.ui, "spinBoxTextSize"):
            return
        size = self._auto_text_size_for_page(self.document[self.current_page_index])
        sb = self.ui.spinBoxTextSize
        old_block = sb.blockSignals(True)
        sb.setValue(size)
        sb.blockSignals(old_block)
        self.default_text_size_pt = int(size)

    def _auto_text_size_for_page(self, page: Any) -> int:
        """Scale text from an A4 reference: A4=15 pt, A3≈21, A2≈30, A1≈42, A0≈60."""
        w_mm = float(page.rect.width) * MM_PER_INCH / PDF_POINTS_PER_INCH
        h_mm = float(page.rect.height) * MM_PER_INCH / PDF_POINTS_PER_INCH
        a4_area = 210.0 * 297.0
        page_area = max(1.0, w_mm * h_mm)
        scale = math.sqrt(page_area / a4_area)
        size = int(round(self.base_text_size_a4_pt * scale))
        return max(self.min_auto_text_size_pt, min(self.max_auto_text_size_pt, size))

    # ======================================================================
    # Page geometry & status bar
    # ======================================================================

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
        parts = [
            f"Page {self.current_page_index + 1}/{self.document.page_count}",
            self._paper_size_text(page),
            f"Zoom {self.current_zoom * 100:.0f}%",
            f"Render {self.current_render_dpi:.0f} DPI",
            f"Text {self.current_text_size_pt()} pt",
            f"Tool: {self.current_tool.title() if self.current_tool else 'Select'}",
        ]
        self.paper_size_label.setText("  |  ".join(parts))

    def _paper_size_text(self, page: Any) -> str:
        w_pt = float(page.rect.width)
        h_pt = float(page.rect.height)
        w_mm = w_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        h_mm = h_pt * MM_PER_INCH / PDF_POINTS_PER_INCH
        name = self._match_paper_name(w_mm, h_mm)
        orient = "Landscape" if w_mm > h_mm else "Portrait"
        w_in = w_mm / MM_PER_INCH
        h_in = h_mm / MM_PER_INCH
        return (
            f"{name} {orient} "
            f"({w_mm:.1f}×{h_mm:.1f} mm / {w_in:.2f}×{h_in:.2f} in)"
        )

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


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    from PySide6.QtCore import Qt

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)

    if len(sys.argv) >= 3:
        input_pdf = sys.argv[1]
        out_path = sys.argv[2]
    else:
        QMessageBox.information(
            None,
            "Usage",
            "python pdf_editor_controller.py  input.pdf  output_folder_or_output.pdf",
        )
        return 1

    editor = PdfCorrectionEditor(input_pdf, out_path)
    editor.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
