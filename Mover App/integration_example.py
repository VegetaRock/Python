"""Example: call FileMoveDialog from an existing PySide6 window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMainWindow

try:
    from .file_move_dialog import FileMoveDialog
    from .mover_core import MoveResult
except ImportError:
    from file_move_dialog import FileMoveDialog
    from mover_core import MoveResult


class YourMainWindow(QMainWindow):
    def move_project_folder(
        self,
        folder_path: str | Path,
        destination_dir: str | Path,
        postgres_dsn: str | None = None,
    ) -> MoveResult | None:
        """Modal use: returns after the user closes the operation dialog."""
        dialog = FileMoveDialog(
            source_folder=folder_path,
            destination_dir=destination_dir,
            parent=self,
            postgres_dsn=postgres_dsn,
            keep_destination_backup=True,
        )
        dialog.exec()

        if dialog.move_result is not None:
            return dialog.move_result

        # The dialog already displays the full error or cancellation details.
        # These properties are available if the calling software needs them.
        if dialog.error_message:
            print(dialog.error_message)
            print(dialog.error_details or "")
        elif dialog.cancelled_message:
            print(dialog.cancelled_message)
        return None

    def move_project_folder_non_modal(
        self,
        folder_path: str | Path,
        destination_dir: str | Path,
        postgres_dsn: str | None = None,
    ) -> None:
        """Non-modal use: keep a reference until the dialog closes."""
        self.file_move_dialog = FileMoveDialog(
            source_folder=folder_path,
            destination_dir=destination_dir,
            parent=self,
            postgres_dsn=postgres_dsn,
        )
        self.file_move_dialog.move_succeeded.connect(self.on_move_completed)
        self.file_move_dialog.move_failed.connect(self.on_move_failed)
        self.file_move_dialog.show()

    def on_move_completed(self, result: MoveResult) -> None:
        print(f"Move completed: {result.destination_project}")

    def on_move_failed(self, message: str, details: str) -> None:
        print(f"Move failed: {message}")
