from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from move_merge_engine import (
    MoveMergeHooks,
    OperationCancelled,
    SafeMoveMergeEngine,
)


class MoveMergeWorker(QObject):
    progress_changed = Signal(int)
    stage_changed = Signal(str)
    log_message = Signal(str)

    succeeded = Signal(object)
    cancelled = Signal(object)
    failed = Signal(str)
    done = Signal()

    def __init__(
        self,
        source_folder: str | os.PathLike[str],
        destination_root: str | os.PathLike[str],
        sheet_name: str,
        *,
        hooks: Optional[MoveMergeHooks] = None,
        strict_verification: bool = True,
    ) -> None:
        super().__init__()
        self.source_folder = str(source_folder)
        self.destination_root = str(destination_root)
        self.sheet_name = str(sheet_name)
        self.hooks = hooks or MoveMergeHooks()
        self.strict_verification = strict_verification
        self.cancel_event = threading.Event()
        self._engine: Optional[SafeMoveMergeEngine] = None

    def request_cancel(self) -> None:
        # threading.Event.set() is safe to call directly from the GUI thread.
        self.cancel_event.set()
        if self._engine is not None:
            self._engine.request_cancel()

    @Slot()
    def run(self) -> None:
        try:
            self._engine = SafeMoveMergeEngine(
                source_folder=self.source_folder,
                destination_root=self.destination_root,
                sheet_name=self.sheet_name,
                hooks=self.hooks,
                on_log=self.log_message.emit,
                on_progress=self.progress_changed.emit,
                on_stage=self.stage_changed.emit,
                cancel_event=self.cancel_event,
                strict_verification=self.strict_verification,
            )
            result = self._engine.run()
            result_dict = result.to_dict()
            if result.status == "cancelled_after_safe_copy":
                self.cancelled.emit(result_dict)
            else:
                self.succeeded.emit(result_dict)
        except OperationCancelled as exc:
            self.cancelled.emit(
                {
                    "status": "cancelled_before_commit",
                    "message": str(exc),
                    "source_folder": self.source_folder,
                    "destination_root": self.destination_root,
                    "sheet_name": self.sheet_name,
                }
            )
        except Exception:
            self.failed.emit(traceback.format_exc())
        finally:
            self._engine = None
            self.done.emit()


class MoveMergeDialog(QDialog):
    """Responsive PySide6 dialog for SafeMoveMergeEngine."""

    operation_completed = Signal(object)
    operation_cancelled = Signal(object)
    operation_failed = Signal(str)

    def __init__(
        self,
        source_folder: str | os.PathLike[str],
        destination_root: str | os.PathLike[str],
        sheet_name: str,
        *,
        hooks: Optional[MoveMergeHooks] = None,
        strict_verification: bool = True,
        auto_start: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.source_folder = Path(source_folder)
        self.destination_root = Path(destination_root)
        self.destination_folder = self.destination_root / self.source_folder.name
        self.sheet_name = str(sheet_name)
        self.hooks = hooks or MoveMergeHooks()
        self.strict_verification = strict_verification

        self._thread: Optional[QThread] = None
        self._worker: Optional[MoveMergeWorker] = None
        self._running = False
        self._result_received = False

        self.setWindowTitle(f"Move and Merge - {self.source_folder.name}")
        self.setModal(True)
        self.resize(840, 560)
        self.setMinimumSize(680, 440)
        self._build_ui()
        self._apply_light_style()

        if auto_start:
            QTimer.singleShot(0, self.start_operation)

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(10)

        title = QLabel("Safe Move and Merge")
        title.setObjectName("titleLabel")
        root_layout.addWidget(title)

        source_label = QLabel(f"Source: {self.source_folder}")
        source_label.setTextInteractionFlags(source_label.textInteractionFlags())
        source_label.setWordWrap(True)
        root_layout.addWidget(source_label)

        destination_label = QLabel(f"Destination: {self.destination_folder}")
        destination_label.setWordWrap(True)
        root_layout.addWidget(destination_label)

        sheet_label = QLabel(f"Sheet name: {self.sheet_name}")
        root_layout.addWidget(sheet_label)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        root_layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        root_layout.addWidget(self.progress_bar)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(8000)
        self.log_box.setPlaceholderText("Operation details will appear here...")
        root_layout.addWidget(self.log_box, 1)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_operation)
        button_layout.addWidget(self.start_button)

        self.cancel_button = QPushButton("Cancel Safely")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        button_layout.addWidget(self.cancel_button)

        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(True)
        self.close_button.clicked.connect(self.accept)
        button_layout.addWidget(self.close_button)

        root_layout.addLayout(button_layout)

    def _apply_light_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background: #f5f7fa;
                color: #1f2937;
            }
            QLabel#titleLabel {
                font-size: 20px;
                font-weight: 700;
                color: #172033;
            }
            QLabel#statusLabel {
                background: #ffffff;
                border: 1px solid #d8dee8;
                border-radius: 6px;
                padding: 8px;
                font-weight: 600;
            }
            QPlainTextEdit {
                background: #ffffff;
                border: 1px solid #cfd7e3;
                border-radius: 6px;
                padding: 6px;
                font-family: Consolas, "Courier New", monospace;
                selection-background-color: #cfe3ff;
            }
            QProgressBar {
                min-height: 22px;
                background: #e7ebf1;
                border: 1px solid #cbd3df;
                border-radius: 6px;
                text-align: center;
                font-weight: 600;
            }
            QProgressBar::chunk {
                background: #2f7ae5;
                border-radius: 5px;
            }
            QPushButton {
                min-width: 105px;
                min-height: 34px;
                padding: 4px 12px;
                border-radius: 6px;
                border: 1px solid #b8c2d1;
                background: #ffffff;
                color: #1f2937;
                font-weight: 600;
            }
            QPushButton:hover:enabled {
                background: #edf4ff;
                border-color: #2f7ae5;
            }
            QPushButton:disabled {
                color: #8b95a5;
                background: #eef1f5;
            }
            QPushButton#cancelButton:enabled {
                color: #a61b1b;
                border-color: #d9a4a4;
            }
            """
        )

    @Slot()
    def start_operation(self) -> None:
        if self._running:
            return

        self._running = True
        self._result_received = False
        self.progress_bar.setValue(0)
        self.log_box.clear()
        self.status_label.setText("Starting...")
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.close_button.setEnabled(False)

        self._thread = QThread(self)
        self._worker = MoveMergeWorker(
            source_folder=self.source_folder,
            destination_root=self.destination_root,
            sheet_name=self.sheet_name,
            hooks=self.hooks,
            strict_verification=self.strict_verification,
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress_changed.connect(self.progress_bar.setValue)
        self._worker.stage_changed.connect(self.status_label.setText)
        self._worker.log_message.connect(self._append_log)
        self._worker.succeeded.connect(self._on_success)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.failed.connect(self._on_failure)
        self._worker.done.connect(self._thread.quit)
        self._worker.done.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    @Slot()
    def request_cancel(self) -> None:
        if not self._running or self._worker is None:
            return
        self.cancel_button.setEnabled(False)
        self.status_label.setText(
            "Cancellation requested. Waiting for a safe stopping point..."
        )
        self._append_log(
            "Cancellation requested. The worker will either roll back the destination "
            "or stop source deletion after the verified copy is safe."
        )
        self._worker.request_cancel()

    @Slot(str)
    def _append_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log_box.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot(object)
    def _on_success(self, result: dict) -> None:
        self._result_received = True
        self.progress_bar.setValue(100)
        status = result.get("status", "completed")
        if status == "completed":
            self.status_label.setText("Completed successfully")
            self._append_log("Operation completed successfully.")
        else:
            remaining = result.get("remaining_item_count", 0)
            self.status_label.setText(
                f"Copy completed safely; {remaining} source item(s) remain"
            )
            self._append_log(
                "Destination is verified, but the source folder was not fully removed. "
                "The PostgreSQL hook was called."
            )
        self.operation_completed.emit(result)

    @Slot(object)
    def _on_cancelled(self, result: dict) -> None:
        self._result_received = True
        status = result.get("status", "cancelled")
        if status == "cancelled_after_safe_copy":
            self.progress_bar.setValue(100)
            self.status_label.setText(
                "Cancelled after safe copy; source leftovers were retained"
            )
        else:
            self.status_label.setText("Cancelled safely; source was not deleted")
        self._append_log(result.get("message", "Operation cancelled safely."))
        self.operation_cancelled.emit(result)

    @Slot(str)
    def _on_failure(self, error_text: str) -> None:
        self._result_received = True
        self.status_label.setText("Operation failed safely")
        self._append_log(error_text)
        self.operation_failed.emit(error_text)
        last_line = next(
            (line.strip() for line in reversed(error_text.splitlines()) if line.strip()),
            "Unknown error",
        )
        QMessageBox.critical(
            self,
            "Move and Merge Failed",
            "The operation stopped. Source deletion was not started unless the "
            "destination had already been fully verified.\n\n"
            f"Error: {last_line}",
        )

    @Slot()
    def _on_thread_finished(self) -> None:
        self._running = False
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        if not self._result_received:
            self.status_label.setText("Worker stopped")
        self._worker = None
        self._thread = None

    def reject(self) -> None:
        if self._running:
            self.request_cancel()
            return
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._running:
            answer = QMessageBox.question(
                self,
                "Operation Running",
                "The operation is still running. Request a safe cancellation?\n\n"
                "The dialog will remain open until rollback or safe stopping is complete.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.request_cancel()
            event.ignore()
            return
        super().closeEvent(event)
