import sys
from pathlib import Path
from datetime import datetime


# Works with PyQt6 or PySide6.
try:
    from PyQt6.QtCore import Qt, QUrl, QFileInfo
    from PyQt6.QtGui import QDesktopServices
    from PyQt6.QtWidgets import (
        QApplication,
        QMainWindow,
        QWidget,
        QVBoxLayout,
        QHBoxLayout,
        QLineEdit,
        QPushButton,
        QFileDialog,
        QSplitter,
        QTreeWidget,
        QTreeWidgetItem,
        QMessageBox,
        QStatusBar,
        QFileIconProvider,
        QHeaderView,
    )
except ImportError:
    try:
        from PySide6.QtCore import Qt, QUrl, QFileInfo
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import (
            QApplication,
            QMainWindow,
            QWidget,
            QVBoxLayout,
            QHBoxLayout,
            QLineEdit,
            QPushButton,
            QFileDialog,
            QSplitter,
            QTreeWidget,
            QTreeWidgetItem,
            QMessageBox,
            QStatusBar,
            QFileIconProvider,
            QHeaderView,
        )
    except ImportError as exc:
        print("Please install PyQt6 or PySide6 first:")
        print("python -m pip install PyQt6")
        print("or")
        print("python -m pip install PySide6")
        raise SystemExit(1) from exc


USER_ROLE = Qt.ItemDataRole.UserRole


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} PB"


def format_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


class FileExplorer(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Python File Explorer")
        self.resize(1200, 700)

        self.icon_provider = QFileIconProvider()
        self.current_root = Path.home()
        self.current_folder = Path.home()

        self.build_ui()
        self.apply_style()

        self.load_root_folder(self.current_root)

    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(6, 6, 6, 6)
        main_layout.setSpacing(8)
        central.setLayout(main_layout)

        # Top path row
        top_layout = QHBoxLayout()
        top_layout.setSpacing(10)

        self.path_box = QLineEdit()
        self.path_box.setObjectName("pathBox")
        self.path_box.setPlaceholderText("Path")
        self.path_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.path_box.returnPressed.connect(self.load_path_from_box)

        self.select_button = QPushButton("Select")
        self.select_button.setObjectName("selectButton")
        self.select_button.clicked.connect(self.select_folder)

        top_layout.addWidget(self.path_box, stretch=1)
        top_layout.addWidget(self.select_button)

        main_layout.addLayout(top_layout)

        # Main split area
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left folder tree
        self.folder_tree = QTreeWidget()
        self.folder_tree.setObjectName("folderTree")
        self.folder_tree.setHeaderLabel("Folders")
        self.folder_tree.itemExpanded.connect(self.populate_folder_when_expanded)
        self.folder_tree.itemClicked.connect(self.folder_clicked)

        # Right file list
        self.file_list = QTreeWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setColumnCount(4)
        self.file_list.setHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.file_list.itemDoubleClicked.connect(self.open_selected_file)
        self.file_list.setSortingEnabled(True)

        header = self.file_list.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self.splitter.addWidget(self.folder_tree)
        self.splitter.addWidget(self.file_list)
        self.splitter.setSizes([260, 940])

        main_layout.addWidget(self.splitter, stretch=1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)

    def apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: white;
                font-family: Arial;
                font-size: 15px;
            }

            QLineEdit#pathBox {
                border: 2px solid black;
                padding: 8px;
                font-size: 22px;
                background: white;
            }

            QPushButton#selectButton {
                background-color: #4778c8;
                color: white;
                border: 1px solid #2f5597;
                padding: 8px 36px;
                font-size: 22px;
            }

            QPushButton#selectButton:hover {
                background-color: #3f6db8;
            }

            QPushButton#selectButton:pressed {
                background-color: #345d9e;
            }

            QTreeWidget#folderTree,
            QTreeWidget#fileList {
                border: 2px solid black;
                background: white;
                alternate-background-color: #f7f7f7;
            }

            QTreeWidget::item {
                padding: 4px;
            }

            QTreeWidget::item:selected {
                background: #4778c8;
                color: white;
            }

            QHeaderView::section {
                background: #f2f2f2;
                border: 1px solid #c8c8c8;
                padding: 5px;
                font-weight: bold;
            }
            """
        )

    # ----------------------------
    # Folder loading
    # ----------------------------

    def select_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Folder",
            str(self.current_folder),
        )

        if folder:
            self.load_root_folder(Path(folder))

    def load_path_from_box(self):
        text = self.path_box.text().strip()

        if not text:
            return

        self.load_root_folder(Path(text).expanduser())

    def load_root_folder(self, folder_path: Path):
        folder_path = folder_path.resolve()

        if not folder_path.exists():
            QMessageBox.warning(self, "Invalid Path", "This path does not exist.")
            return

        if not folder_path.is_dir():
            QMessageBox.warning(self, "Invalid Path", "This path is not a folder.")
            return

        self.current_root = folder_path
        self.current_folder = folder_path

        self.path_box.setText(str(folder_path))
        self.folder_tree.clear()

        root_item = QTreeWidgetItem([folder_path.name or str(folder_path)])
        root_item.setData(0, USER_ROLE, str(folder_path))
        root_item.setIcon(0, self.icon_provider.icon(QFileInfo(str(folder_path))))

        self.folder_tree.addTopLevelItem(root_item)
        self.add_dummy_child_if_needed(root_item, folder_path)

        root_item.setExpanded(True)
        self.folder_tree.setCurrentItem(root_item)

        self.show_files(folder_path)

    def add_dummy_child_if_needed(self, item: QTreeWidgetItem, folder_path: Path):
        if self.folder_has_subfolders(folder_path):
            dummy = QTreeWidgetItem(["Loading..."])
            dummy.setData(0, USER_ROLE, None)
            item.addChild(dummy)

    def folder_has_subfolders(self, folder_path: Path) -> bool:
        try:
            for child in folder_path.iterdir():
                try:
                    if child.is_dir():
                        return True
                except Exception:
                    continue
        except Exception:
            return False

        return False

    def populate_folder_when_expanded(self, item: QTreeWidgetItem):
        path_text = item.data(0, USER_ROLE)

        if not path_text:
            return

        folder_path = Path(path_text)

        # Already populated
        if item.childCount() > 0:
            first_child = item.child(0)
            if first_child.data(0, USER_ROLE) is not None:
                return

        item.takeChildren()

        try:
            folders = []

            for child in folder_path.iterdir():
                try:
                    if child.is_dir():
                        folders.append(child)
                except Exception:
                    continue

            folders.sort(key=lambda p: p.name.lower())

            for folder in folders:
                child_item = QTreeWidgetItem([folder.name])
                child_item.setData(0, USER_ROLE, str(folder))
                child_item.setIcon(0, self.icon_provider.icon(QFileInfo(str(folder))))

                item.addChild(child_item)
                self.add_dummy_child_if_needed(child_item, folder)

        except PermissionError:
            self.status.showMessage(f"Permission denied: {folder_path}")
        except Exception as exc:
            self.status.showMessage(f"Error reading folder: {exc}")

    def folder_clicked(self, item: QTreeWidgetItem):
        path_text = item.data(0, USER_ROLE)

        if not path_text:
            return

        folder_path = Path(path_text)

        if folder_path.exists() and folder_path.is_dir():
            self.current_folder = folder_path
            self.path_box.setText(str(folder_path))
            self.show_files(folder_path)

    # ----------------------------
    # File list
    # ----------------------------

    def show_files(self, folder_path: Path):
        self.file_list.setSortingEnabled(False)
        self.file_list.clear()

        files = []

        try:
            for child in folder_path.iterdir():
                try:
                    if child.is_file():
                        files.append(child)
                except Exception:
                    continue

            files.sort(key=lambda p: p.name.lower())

            for file_path in files:
                try:
                    stat_info = file_path.stat()

                    extension = file_path.suffix.replace(".", "").upper()
                    file_type = f"{extension} File" if extension else "File"

                    item = QTreeWidgetItem(
                        [
                            file_path.name,
                            file_type,
                            human_size(stat_info.st_size),
                            format_time(stat_info.st_mtime),
                        ]
                    )

                    item.setData(0, USER_ROLE, str(file_path))
                    item.setIcon(0, self.icon_provider.icon(QFileInfo(str(file_path))))

                    self.file_list.addTopLevelItem(item)

                except Exception:
                    continue

            self.status.showMessage(
                f"{folder_path}   |   {len(files)} file(s)"
            )

        except PermissionError:
            self.status.showMessage(f"Permission denied: {folder_path}")
        except Exception as exc:
            self.status.showMessage(f"Error reading files: {exc}")

        self.file_list.setSortingEnabled(True)

    def open_selected_file(self, item: QTreeWidgetItem):
        path_text = item.data(0, USER_ROLE)

        if not path_text:
            return

        file_path = Path(path_text)

        if not file_path.exists():
            QMessageBox.warning(self, "File Missing", "This file no longer exists.")
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(file_path)))


def main():
    app = QApplication(sys.argv)
    window = FileExplorer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()