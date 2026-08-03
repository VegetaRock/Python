from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from move_merge_dialog import MoveMergeDialog
from move_merge_engine import MoveMergeHooks


# Configure this once in your application.
APPROVED_MOLDS_ROOT = Path(r"D:\Approved Molds")


class MyPostgresHooks(MoveMergeHooks):
    def log_m_postgress(self, **data) -> None:
        # TODO: Add your PostgreSQL code here.
        # Example available values:
        # data["folder_name"], data["folder_path"], data["destination_path"],
        # data["sheet_name"], data["remaining_items"]
        print("log_m_postgress:", data)

    def log_cn_postgress(self, **data) -> None:
        # TODO: Add your PostgreSQL code here.
        print("log_cn_postgress:", data)


def show_move_merge_dialog(
    source_folder_from_dir2: str | Path,
    sheet_name: str,
    parent=None,
) -> MoveMergeDialog:
    """Use this function from your existing PySide6 main window."""
    dialog = MoveMergeDialog(
        source_folder=source_folder_from_dir2,
        destination_root=APPROVED_MOLDS_ROOT,
        sheet_name=sheet_name,
        hooks=MyPostgresHooks(),
        strict_verification=True,
        auto_start=True,
        parent=parent,
    )
    dialog.operation_completed.connect(
        lambda result: print("Completed:", result)
    )
    dialog.operation_cancelled.connect(
        lambda result: print("Cancelled:", result)
    )
    dialog.operation_failed.connect(
        lambda error: print("Failed:", error)
    )
    return dialog


if __name__ == "__main__":
    app = QApplication(sys.argv)

    # Variable 1: full Dir 2 folder path, including K68567.
    source_folder = Path(r"E:\Variable\K68567")

    # Variable 2: sheet name.
    sheet_name = "R(0)"

    move_dialog = show_move_merge_dialog(source_folder, sheet_name)
    move_dialog.exec()

    sys.exit(0)
