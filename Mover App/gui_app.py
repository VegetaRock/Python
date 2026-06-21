"""Small standalone demo for FileMoveDialog.

Existing PySide6 software should import FileMoveDialog instead of starting another
QApplication. This file is only a convenient manual demo.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    from .file_move_dialog import FileMoveDialog
except ImportError:
    from file_move_dialog import FileMoveDialog


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("File mover integration demo")
        self.resize(760, 230)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        form = QFormLayout()

        self.source_edit = QLineEdit()
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        source_button = QPushButton("Browse…")
        source_button.clicked.connect(self._browse_source)
        source_row.addWidget(source_button)
        form.addRow("Source project folder:", source_row)

        self.destination_edit = QLineEdit()
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_button = QPushButton("Browse…")
        destination_button.clicked.connect(self._browse_destination)
        destination_row.addWidget(destination_button)
        form.addRow("Destination root:", destination_row)

        self.dsn_edit = QLineEdit(os.getenv("FILE_MOVER_PG_DSN", ""))
        self.dsn_edit.setPlaceholderText(
            "postgresql://user:password@server:5432/database (optional)"
        )
        form.addRow("PostgreSQL DSN:", self.dsn_edit)
        layout.addLayout(form)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

        move_button = QPushButton("Move project folder")
        move_button.clicked.connect(self._show_move_dialog)
        layout.addWidget(move_button)

    def _browse_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select source project folder")
        if path:
            self.source_edit.setText(path)

    def _browse_destination(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select destination root")
        if path:
            self.destination_edit.setText(path)

    def _show_move_dialog(self) -> None:
        source = self.source_edit.text().strip()
        destination = self.destination_edit.text().strip()
        if not source or not destination:
            self.status_label.setText("Select both paths first.")
            return

        dialog = FileMoveDialog(
            source_folder=source,
            destination_dir=destination,
            parent=self,
            postgres_dsn=self.dsn_edit.text().strip() or None,
        )
        dialog.exec()

        if dialog.move_result is not None:
            self.status_label.setText(
                f"{dialog.move_result.status}: "
                f"{dialog.move_result.destination_project}"
            )
        elif dialog.error_message:
            self.status_label.setText(f"Failed: {dialog.error_message}")
        elif dialog.cancelled_message:
            self.status_label.setText("Cancelled")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
