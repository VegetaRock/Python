from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFontDatabase, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:  # Package import
    from .mover_core import MoveOptions, MoveResult, OperationCancelled, SafeFileMover
except ImportError:  # Same-directory import
    from mover_core import MoveOptions, MoveResult, OperationCancelled, SafeFileMover


DEFAULT_LOCK_PATTERNS = ("*.lock", "*.lck", "~$*", ".~lock.*#")
DEFAULT_SENSITIVE_MARKERS = ("TRASH", "OTHER")


class _MoveWorker(QObject):
    """Runs SafeFileMover outside the GUI thread."""

    progress = Signal(int, str)
    log = Signal(str)
    succeeded = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, options: MoveOptions) -> None:
        super().__init__()
        self._options = options
        self._mover: SafeFileMover | None = None
        self._cancel_requested = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            self._mover = SafeFileMover(
                self._options,
                progress_callback=self.progress.emit,
                log_callback=self.log.emit,
            )
            if self._cancel_requested.is_set():
                self._mover.request_cancel()
            result = self._mover.move()
            self.succeeded.emit(result)
        except OperationCancelled as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())
        finally:
            self.finished.emit()

    def request_cancel(self) -> None:
        """Thread-safe; may be called directly by the GUI thread."""
        self._cancel_requested.set()
        mover = self._mover
        if mover is not None:
            mover.request_cancel()


class FileMoveDialog(QDialog):
    """
    Reusable PySide6 progress dialog for SafeFileMover.

    Pass the source project folder (for example ``Dir1/123456``) and the
    destination root (``Dir2``). The core creates/updates ``Dir2/123456``.
    The operation starts automatically after the dialog is shown.
    """

    move_succeeded = Signal(object)
    move_cancelled = Signal(str)
    move_failed = Signal(str, str)

    def __init__(
        self,
        source_folder: str | os.PathLike[str],
        destination_dir: str | os.PathLike[str],
        parent: QWidget | None = None,
        *,
        postgres_dsn: str | None = None,
        lock_patterns: Sequence[str] = DEFAULT_LOCK_PATTERNS,
        sensitive_markers: Sequence[str] = DEFAULT_SENSITIVE_MARKERS,
        keep_destination_backup: bool = True,
        preserve_file_extension_when_randomizing: bool = True,
        durable_writes: bool = True,
        copy_retry_count: int = 3,
        database_table: str = "file_move_incomplete_cleanup",
        max_log_lines: int = 0,
        auto_start: bool = True,
        window_title: str = "Verified folder move",
    ) -> None:
        super().__init__(parent)

        self.source_folder = Path(source_folder).expanduser()
        self.destination_dir = Path(destination_dir).expanduser()
        self.options = MoveOptions(
            source_project=self.source_folder,
            destination_root=self.destination_dir,
            postgres_dsn=postgres_dsn,
            lock_patterns=tuple(lock_patterns),
            sensitive_markers=tuple(sensitive_markers),
            keep_destination_backup=keep_destination_backup,
            preserve_file_extension_when_randomizing=(
                preserve_file_extension_when_randomizing
            ),
            durable_writes=durable_writes,
            copy_retry_count=copy_retry_count,
            log_each_item=True,
            database_table=database_table,
        )

        self.move_result: MoveResult | None = None
        self.error_message: str | None = None
        self.error_details: str | None = None
        self.cancelled_message: str | None = None

        self._thread: QThread | None = None
        self._worker: _MoveWorker | None = None
        self._started = False
        self._running = False
        self._terminal_state: str | None = None
        self._auto_start = auto_start
        self._max_log_lines = max(0, max_log_lines)

        self.setWindowTitle(window_title)
        self.setMinimumSize(860, 560)
        self._build_ui()

    @property
    def is_running(self) -> bool:
        return self._running

    def _is_active_or_pending(self) -> bool:
        return self._running or (self._auto_start and not self._started)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        summary = QFormLayout()
        source_label = QLabel(str(self.source_folder))
        source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        source_label.setWordWrap(True)
        summary.addRow("Source project:", source_label)

        final_destination = self.destination_dir / self.source_folder.name
        destination_label = QLabel(str(final_destination))
        destination_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        destination_label.setWordWrap(True)
        summary.addRow("Destination project:", destination_label)
        layout.addLayout(summary)

        self.phase_label = QLabel("Waiting to start")
        self.phase_label.setWordWrap(True)
        self.phase_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.phase_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)

        log_caption = QLabel("Operations")
        layout.addWidget(log_caption)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        if self._max_log_lines:
            self.log_view.setMaximumBlockCount(self._max_log_lines)
        self.log_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        layout.addWidget(self.log_view, 1)

        button_row = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel before commit")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch(1)

        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self._close_after_finish)
        self.close_button.setDefault(True)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._auto_start and not self._started:
            # Start after Qt has painted the dialog at least once.
            QTimer.singleShot(0, self.start)

    @Slot()
    def start(self) -> None:
        """Start the operation. Calling this more than once has no effect."""
        if self._started:
            return
        self._started = True
        self._running = True
        self.cancel_button.setEnabled(True)
        self.close_button.setEnabled(False)
        self.phase_label.setText("Starting")
        self._append_log("Dialog opened; starting the verified move.")
        self._append_log(f"Source: {self.source_folder}")
        self._append_log(f"Destination root: {self.destination_dir}")

        thread = QThread(self)
        worker = _MoveWorker(self.options)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.log.connect(self._append_log)
        worker.succeeded.connect(self._on_succeeded)
        worker.cancelled.connect(self._on_cancelled)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._thread = thread
        self._worker = worker
        thread.start()

    @Slot()
    def request_cancel(self) -> None:
        if not self._running or self._worker is None:
            return
        self._append_log(
            "Cancellation requested. It is accepted only before the destination "
            "commit point; after commit the consistency steps must finish."
        )
        self.cancel_button.setEnabled(False)
        self._worker.request_cancel()

    @Slot(int, str)
    def _on_progress(self, percent: int, operation: str) -> None:
        self.progress_bar.setValue(max(0, min(100, percent)))
        self.phase_label.setText(operation)
        normalized = operation.casefold()
        if "committing destination" in normalized or "committed" in normalized:
            self.cancel_button.setEnabled(False)

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_view.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot(object)
    def _on_succeeded(self, result: MoveResult) -> None:
        self.move_result = result
        self._terminal_state = "success"
        self.cancel_button.setEnabled(False)
        self.progress_bar.setValue(100)
        status = result.status.replace("_", " ").title()
        self.phase_label.setText(status)
        self._append_log(f"RESULT: {status}")
        self._append_log(f"Destination: {result.destination_project}")
        if result.backup_path:
            self._append_log(f"Destination backup: {result.backup_path}")
        self._append_log(f"Operation journal: {result.journal_path}")
        for rename_log in result.renamed_logs:
            self._append_log(f"Rename log: {rename_log}")
        if result.leftover_count:
            self._append_log(f"Source leftovers: {result.leftover_count}")
        if result.database_logged:
            self._append_log("Incomplete-cleanup data was stored in PostgreSQL.")
        if result.fallback_incident_log:
            self._append_log(
                f"Fallback incomplete-cleanup log: {result.fallback_incident_log}"
            )
        for warning in result.warnings:
            self._append_log(f"WARNING: {warning}")
        self.move_succeeded.emit(result)

    @Slot(str)
    def _on_cancelled(self, message: str) -> None:
        self.cancelled_message = message
        self._terminal_state = "cancelled"
        self.cancel_button.setEnabled(False)
        self.phase_label.setText("Cancelled")
        self._append_log(f"CANCELLED: {message}")
        self.move_cancelled.emit(message)

    @Slot(str, str)
    def _on_failed(self, message: str, details: str) -> None:
        self.error_message = message
        self.error_details = details
        self._terminal_state = "failed"
        self.cancel_button.setEnabled(False)
        self.phase_label.setText("Failed")
        self._append_log(f"ERROR: {message}")
        self._append_log(details.rstrip())
        self.move_failed.emit(message, details)

    @Slot()
    def _on_thread_finished(self) -> None:
        self._running = False
        self._thread = None
        self._worker = None
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        if self._terminal_state is None:
            self._terminal_state = "failed"
            self.error_message = "Worker thread stopped without a result."
            self.phase_label.setText("Failed")
            self._append_log(f"ERROR: {self.error_message}")

    @Slot()
    def _close_after_finish(self) -> None:
        if self._running:
            return
        if self.move_result is not None:
            self.accept()
        else:
            self.reject()

    def reject(self) -> None:
        if self._is_active_or_pending():
            self._append_log(
                "The dialog cannot close while the move is running. Cancel before "
                "commit or wait for completion."
            )
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._is_active_or_pending():
            self._append_log(
                "Close request ignored because the move is still running."
            )
            event.ignore()
            return
        super().closeEvent(event)
