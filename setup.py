import os

# Must be set before QApplication is created.
# This tells Qt's Windows platform plugin not to follow Windows dark mode.
if os.name == "nt":
    os.environ["QT_QPA_PLATFORM"] = "windows:darkmode=0"

import sys
import stat
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal, QTimer, QStandardPaths
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QWizard,
    QWizardPage,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QProgressBar,
    QTextEdit,
    QFileDialog,
    QMessageBox,
    QFrame,
)


# ==========================================================
# CHANGE THESE VALUES FOR YOUR APP
# ==========================================================
APP_NAME = "MyApp"
APP_EXE = "MyApp.exe"
COMPANY_NAME = "YourCompany"

APP_FILES_FOLDER = "AppFiles"
APP_ICON = "app_icon.ico"


# ==========================================================
# LIGHT THEME HELPERS
# ==========================================================
def force_light_theme(app: QApplication) -> None:
    """
    Forces Qt widgets to use a light theme even when Windows is in dark mode.
    """
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#202124"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f6f8fa"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#202124"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#202124"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#f7f7f7"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#202124"))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#d8e9ff"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#111111"))

    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.WindowText,
        QColor("#8a8a8a"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor("#8a8a8a"),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor("#8a8a8a"),
    )

    app.setPalette(palette)


def force_light_title_bar(widget) -> None:
    """
    Tries to keep the native Windows title bar light too.
    Safe to ignore if Windows does not support this setting.
    """
    if os.name != "nt":
        return

    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(0)

        # 20 is used by current Windows 10/11 builds.
        # 19 is the older attribute number, kept as fallback.
        for attribute in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
            except Exception:
                pass
    except Exception:
        pass


# ==========================================================
# PATH HELPERS
# ==========================================================
def resource_path(filename: str) -> Path:
    """
    Finds resource files in normal Python mode and PyInstaller onefile mode.
    Used for app_icon.ico.
    """
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / filename)

        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / filename)

    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / filename)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def find_appfiles_dir() -> Path:
    """
    Finds AppFiles folder in:
    1. normal Python mode
    2. PyInstaller onefile mode
    3. PyInstaller onedir mode
    """
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / APP_FILES_FOLDER)

        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / APP_FILES_FOLDER)

    script_dir = Path(__file__).resolve().parent
    candidates.append(script_dir / APP_FILES_FOLDER)

    for candidate in candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    return candidates[0]


def default_install_dir() -> Path:
    """
    Default install location.
    Uses D drive if available, otherwise uses current-user local app folder.
    """
    d_drive = Path("D:/")

    if d_drive.exists():
        return d_drive / APP_NAME

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Programs" / APP_NAME

    return Path.home() / APP_NAME


def get_desktop_dir() -> Path:
    """
    Gets the current user's Desktop folder.
    """
    desktop = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DesktopLocation
    )

    if desktop:
        return Path(desktop)

    return Path.home() / "Desktop"


def check_writable_install_location(target_dir: Path) -> tuple[bool, str]:
    """
    Checks whether setup can write to the selected location without admin.
    """
    try:
        existing_parent = target_dir

        while not existing_parent.exists():
            if existing_parent.parent == existing_parent:
                return False, "Drive or parent folder does not exist."
            existing_parent = existing_parent.parent

        if not existing_parent.is_dir():
            return False, "Selected parent path is not a folder."

        fd, temp_name = tempfile.mkstemp(
            prefix="setup_write_test_",
            dir=str(existing_parent),
        )
        os.close(fd)
        Path(temp_name).unlink(missing_ok=True)

        return True, ""

    except PermissionError:
        return False, "No write permission for this location."
    except Exception as err:
        return False, str(err)


def paths_are_same(path1: Path, path2: Path) -> bool:
    try:
        return path1.resolve() == path2.resolve()
    except Exception:
        return False


# ==========================================================
# WINDOWS SHORTCUT CREATION
# ==========================================================
def create_windows_shortcut(
    target_path: Path,
    shortcut_path: Path,
    working_dir: Path,
    icon_path: Optional[Path] = None,
) -> None:
    """
    Creates a Windows .lnk shortcut using PowerShell + WScript.Shell.
    No admin permission and no pywin32 dependency required.
    """
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)

    ps_script = r'''
param(
    [string]$TargetPath,
    [string]$ShortcutPath,
    [string]$WorkingDirectory,
    [string]$IconLocation
)

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $TargetPath
$Shortcut.WorkingDirectory = $WorkingDirectory

if ($IconLocation -ne "") {
    $Shortcut.IconLocation = $IconLocation
}

$Shortcut.Save()
'''

    with tempfile.TemporaryDirectory() as temp_dir:
        script_path = Path(temp_dir) / "create_shortcut.ps1"
        script_path.write_text(ps_script, encoding="utf-8")

        creation_flags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW

        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                "-TargetPath",
                str(target_path),
                "-ShortcutPath",
                str(shortcut_path),
                "-WorkingDirectory",
                str(working_dir),
                "-IconLocation",
                str(icon_path or target_path),
            ],
            capture_output=True,
            text=True,
            creationflags=creation_flags,
        )

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(message or "Failed to create desktop shortcut.")


# ==========================================================
# INSTALLER WORKER THREAD
# ==========================================================
class InstallerWorker(QThread):
    progress = Signal(int)
    status = Signal(str)
    log = Signal(str)
    success = Signal(str)
    failure = Signal(str)

    def __init__(
        self,
        source_dir: Path,
        install_dir: Path,
        create_desktop_shortcut: bool,
        desktop_dir: Path,
    ):
        super().__init__()

        self.source_dir = source_dir
        self.install_dir = install_dir
        self.create_desktop_shortcut = create_desktop_shortcut
        self.desktop_dir = desktop_dir

    def run(self):
        try:
            if os.name != "nt":
                raise RuntimeError("This installer is designed for Windows only.")

            if not self.source_dir.exists():
                raise RuntimeError(
                    f"{APP_FILES_FOLDER} folder not found:\n\n"
                    f"{self.source_dir}\n\n"
                    f"Keep {APP_FILES_FOLDER} beside setup_ui.py, "
                    f"or bundle it using PyInstaller --add-data."
                )

            source_exe = self.source_dir / APP_EXE

            if not source_exe.exists():
                raise RuntimeError(
                    f"{APP_EXE} not found inside:\n\n"
                    f"{self.source_dir}\n\n"
                    f"Put your PyInstaller dist folder contents inside "
                    f"{APP_FILES_FOLDER}."
                )

            self.status.emit("Preparing installation...")
            self.log.emit(f"Application: {APP_NAME}")
            self.log.emit(f"Publisher: {COMPANY_NAME}")
            self.log.emit(f"Source folder: {self.source_dir}")
            self.log.emit(f"Install folder: {self.install_dir}")
            self.log.emit("")

            self.install_dir.mkdir(parents=True, exist_ok=True)

            all_files: list[Path] = []
            for root, _, filenames in os.walk(self.source_dir):
                root_path = Path(root)
                for filename in filenames:
                    all_files.append(root_path / filename)

            total_files = max(len(all_files), 1)

            for index, source_file in enumerate(all_files, start=1):
                relative_path = source_file.relative_to(self.source_dir)
                destination_file = self.install_dir / relative_path

                if paths_are_same(source_file, destination_file):
                    continue

                destination_file.parent.mkdir(parents=True, exist_ok=True)

                if destination_file.exists():
                    try:
                        destination_file.chmod(stat.S_IWRITE)
                    except OSError:
                        pass

                shutil.copy2(source_file, destination_file)

                percent = int((index / total_files) * 88)
                self.progress.emit(percent)

                if index == 1 or index % 5 == 0 or index == total_files:
                    self.status.emit(f"Copying: {relative_path}")

            shortcut_icon = self.install_dir / APP_EXE
            icon_source = resource_path(APP_ICON)

            if icon_source.exists():
                try:
                    installed_icon = self.install_dir / APP_ICON
                    if not paths_are_same(icon_source, installed_icon):
                        shutil.copy2(icon_source, installed_icon)
                    shortcut_icon = installed_icon
                    self.log.emit(f"Icon copied: {installed_icon}")
                except Exception as icon_err:
                    self.log.emit(f"Icon copy skipped: {icon_err}")

            if self.create_desktop_shortcut:
                self.status.emit("Creating desktop shortcut...")
                self.progress.emit(95)

                shortcut_path = self.desktop_dir / f"{APP_NAME}.lnk"
                installed_exe = self.install_dir / APP_EXE

                create_windows_shortcut(
                    target_path=installed_exe,
                    shortcut_path=shortcut_path,
                    working_dir=self.install_dir,
                    icon_path=shortcut_icon,
                )

                self.log.emit(f"Desktop shortcut created: {shortcut_path}")

            self.progress.emit(100)
            self.status.emit("Installation completed successfully.")
            self.success.emit(str(self.install_dir))

        except Exception as err:
            self.failure.emit(str(err))


# ==========================================================
# UI PAGES
# ==========================================================
class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()

        self.setTitle(f"Welcome to {APP_NAME} Setup")
        self.setSubTitle(f"This wizard will install {APP_NAME} on your computer.")

        heading = QLabel(f"Install {APP_NAME}")
        heading.setObjectName("PageHeading")

        info = QLabel(
            f"Setup will copy {APP_NAME} files to your selected folder and create "
            f"a desktop shortcut.\n\n"
            f"This installer does not need admin permission when the selected "
            f"install folder is writable."
        )
        info.setObjectName("BodyText")
        info.setWordWrap(True)

        install_location_title = QLabel("Default install location:")
        install_location_title.setObjectName("SmallTitle")

        install_location = QLabel(str(default_install_dir()))
        install_location.setObjectName("PathText")
        install_location.setWordWrap(True)

        card = QFrame()
        card.setObjectName("InfoCard")

        card_layout = QVBoxLayout()
        card_layout.addWidget(install_location_title)
        card_layout.addWidget(install_location)
        card.setLayout(card_layout)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        layout = QVBoxLayout()
        layout.addWidget(heading)
        layout.addSpacing(8)
        layout.addWidget(info)
        layout.addSpacing(16)
        layout.addWidget(card)
        layout.addSpacing(16)
        layout.addWidget(line)
        layout.addStretch()

        self.setLayout(layout)


class LocationPage(QWizardPage):
    def __init__(self):
        super().__init__()

        self.setTitle("Choose Install Location")
        self.setSubTitle("Choose where the application files should be copied.")

        install_label = QLabel("Install folder:")
        install_label.setObjectName("SmallTitle")

        self.path_edit = QLineEdit(str(default_install_dir()))
        self.path_edit.setMinimumWidth(430)

        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.browse_folder)

        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit)
        path_row.addWidget(browse_button)

        self.desktop_shortcut_checkbox = QCheckBox("Create desktop shortcut")
        self.desktop_shortcut_checkbox.setChecked(True)

        self.launch_checkbox = QCheckBox(f"Launch {APP_NAME} after setup")
        self.launch_checkbox.setChecked(True)

        note = QLabel(
            "Important: D drive installation works without admin only when "
            "your Windows user has write permission to the selected folder."
        )
        note.setObjectName("BodyText")
        note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(install_label)
        layout.addLayout(path_row)
        layout.addSpacing(14)
        layout.addWidget(self.desktop_shortcut_checkbox)
        layout.addWidget(self.launch_checkbox)
        layout.addSpacing(14)
        layout.addWidget(note)
        layout.addStretch()

        self.setLayout(layout)

        self.registerField("install_dir*", self.path_edit)
        self.registerField("desktop_shortcut", self.desktop_shortcut_checkbox)
        self.registerField("launch_after", self.launch_checkbox)

        self.setButtonText(QWizard.WizardButton.NextButton, "Install")

    def browse_folder(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select install folder",
            self.path_edit.text(),
        )

        if selected:
            self.path_edit.setText(selected)

    def validatePage(self) -> bool:
        raw_path = self.path_edit.text().strip().strip('"')

        if not raw_path:
            QMessageBox.warning(
                self,
                "Invalid location",
                "Please select an install folder.",
            )
            return False

        install_dir = Path(raw_path)

        if install_dir.exists() and not install_dir.is_dir():
            QMessageBox.warning(
                self,
                "Invalid location",
                "Selected path exists but it is not a folder.",
            )
            return False

        ok, message = check_writable_install_location(install_dir)

        if not ok:
            QMessageBox.warning(
                self,
                "No write permission",
                f"Setup cannot write to this location:\n\n"
                f"{install_dir}\n\n"
                f"{message}",
            )
            return False

        self.path_edit.setText(str(install_dir))
        return True


class InstallingPage(QWizardPage):
    def __init__(self):
        super().__init__()

        self.setTitle("Installing")
        self.setSubTitle("Please wait while setup copies files.")

        self.status_label = QLabel("Starting installation...")
        self.status_label.setObjectName("BodyText")
        self.status_label.setWordWrap(True)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(170)

        layout = QVBoxLayout()
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_box)

        self.setLayout(layout)

        self._complete = False
        self._started = False
        self.worker: Optional[InstallerWorker] = None

    def initializePage(self):
        if self._started:
            return

        self._started = True
        self._complete = False

        self.progress_bar.setValue(0)
        self.log_box.clear()
        self.status_label.setText("Starting installation...")

        wizard = self.wizard()

        wizard.button(QWizard.WizardButton.BackButton).setEnabled(False)
        wizard.button(QWizard.WizardButton.NextButton).setEnabled(False)
        wizard.button(QWizard.WizardButton.CancelButton).setEnabled(False)

        source_dir = find_appfiles_dir()
        install_dir = Path(str(self.field("install_dir")))
        create_shortcut = bool(self.field("desktop_shortcut"))
        desktop_dir = get_desktop_dir()

        self.worker = InstallerWorker(
            source_dir=source_dir,
            install_dir=install_dir,
            create_desktop_shortcut=create_shortcut,
            desktop_dir=desktop_dir,
        )

        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.status.connect(self.status_label.setText)
        self.worker.log.connect(self.log_box.append)
        self.worker.success.connect(self.install_success)
        self.worker.failure.connect(self.install_failure)

        self.worker.start()

    def isComplete(self) -> bool:
        return self._complete

    def install_success(self, install_dir: str):
        wizard = self.wizard()

        wizard.install_succeeded = True
        wizard.installed_dir = install_dir

        self._complete = True
        self.completeChanged.emit()

        wizard.button(QWizard.WizardButton.NextButton).setEnabled(True)
        wizard.button(QWizard.WizardButton.CancelButton).setEnabled(True)

        QTimer.singleShot(700, wizard.next)

    def install_failure(self, message: str):
        wizard = self.wizard()

        wizard.install_succeeded = False
        self._started = False
        self._complete = False

        self.status_label.setText("Installation failed.")
        self.log_box.append("")
        self.log_box.append("ERROR:")
        self.log_box.append(message)

        QMessageBox.critical(
            self,
            "Installation failed",
            message,
        )

        wizard.button(QWizard.WizardButton.CancelButton).setEnabled(True)
        wizard.button(QWizard.WizardButton.BackButton).setEnabled(True)


class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()

        self.setTitle("Setup Complete")
        self.setSubTitle(f"{APP_NAME} has been installed.")

        self.message_label = QLabel()
        self.message_label.setObjectName("BodyText")
        self.message_label.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addWidget(self.message_label)
        layout.addStretch()

        self.setLayout(layout)

    def initializePage(self):
        wizard = self.wizard()

        install_dir = getattr(
            wizard,
            "installed_dir",
            str(self.field("install_dir")),
        )

        if bool(self.field("desktop_shortcut")):
            shortcut_message = "A desktop shortcut was created."
        else:
            shortcut_message = "Desktop shortcut was not selected."

        self.message_label.setText(
            f"{APP_NAME} has been installed successfully.\n\n"
            f"Installed location:\n"
            f"{install_dir}\n\n"
            f"{shortcut_message}\n\n"
            f"Click Finish to close setup."
        )

        wizard.button(QWizard.WizardButton.FinishButton).setEnabled(True)


# ==========================================================
# MAIN WIZARD
# ==========================================================
class InstallerWizard(QWizard):
    def __init__(self):
        super().__init__()

        self.install_succeeded = False
        self.installed_dir = ""

        self.setWindowTitle(f"{APP_NAME} Setup")
        self.setWindowIcon(QIcon(str(resource_path(APP_ICON))))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)

        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.NoBackButtonOnLastPage, True)

        self.addPage(WelcomePage())
        self.addPage(LocationPage())
        self.addPage(InstallingPage())
        self.addPage(FinishPage())

        self.resize(700, 470)

        self.setStyleSheet("""
            QWizard {
                background-color: #ffffff;
                color: #202124;
            }

            QWizard QWidget {
                background-color: #ffffff;
                color: #202124;
            }

            QWizardPage {
                background-color: #ffffff;
                color: #202124;
            }

            QWizard QLabel {
                background-color: transparent;
                color: #202124;
                font-size: 13px;
            }

            QLabel#PageHeading {
                color: #111111;
                font-size: 22px;
                font-weight: bold;
            }

            QLabel#BodyText {
                color: #333333;
                font-size: 13px;
            }

            QLabel#SmallTitle {
                color: #111111;
                font-size: 13px;
                font-weight: bold;
            }

            QLabel#PathText {
                color: #0b5394;
                font-size: 13px;
            }

            QFrame#InfoCard {
                background-color: #f6f8fa;
                border: 1px solid #d0d7de;
                border-radius: 6px;
                padding: 8px;
            }

            QFrame#InfoCard QLabel {
                background-color: transparent;
            }

            QLineEdit {
                color: #202124;
                background-color: #ffffff;
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 6px;
                font-size: 13px;
            }

            QCheckBox {
                color: #202124;
                background-color: #ffffff;
                font-size: 13px;
            }

            QPushButton {
                color: #202124;
                background-color: #f7f7f7;
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 13px;
            }

            QPushButton:hover {
                background-color: #eeeeee;
            }

            QPushButton:pressed {
                background-color: #e0e0e0;
            }

            QPushButton:default {
                background-color: #e8f0fe;
                border: 1px solid #1a73e8;
            }

            QPushButton:disabled {
                color: #8a8a8a;
                background-color: #f1f1f1;
                border: 1px solid #d0d0d0;
            }

            QProgressBar {
                color: #202124;
                background-color: #ffffff;
                border: 1px solid #b8b8b8;
                border-radius: 4px;
                height: 22px;
                text-align: center;
            }

            QProgressBar::chunk {
                background-color: #3b82f6;
                border-radius: 3px;
            }

            QTextEdit {
                color: #202124;
                background-color: #ffffff;
                border: 1px solid #b8b8b8;
                font-family: Consolas, monospace;
                font-size: 12px;
            }
        """)

    def reject(self):
        current_page = self.currentPage()

        if isinstance(current_page, InstallingPage):
            if current_page.worker is not None and current_page.worker.isRunning():
                QMessageBox.information(
                    self,
                    "Installation running",
                    "Setup is currently copying files. Please wait until it finishes.",
                )
                return

        super().reject()

    def accept(self):
        if self.install_succeeded and bool(self.field("launch_after")):
            install_dir = Path(self.installed_dir)
            exe_path = install_dir / APP_EXE

            if exe_path.exists():
                try:
                    subprocess.Popen(
                        [str(exe_path)],
                        cwd=str(install_dir),
                    )
                except Exception as err:
                    QMessageBox.warning(
                        self,
                        "Launch failed",
                        f"{APP_NAME} was installed, but setup could not launch it.\n\n"
                        f"{err}",
                    )

        super().accept()


# ==========================================================
# APP ENTRY POINT
# ==========================================================
def main():
    app = QApplication(sys.argv)

    force_light_theme(app)

    app.setApplicationName(f"{APP_NAME} Setup")
    app.setOrganizationName(COMPANY_NAME)
    app.setWindowIcon(QIcon(str(resource_path(APP_ICON))))

    wizard = InstallerWizard()
    wizard.show()
    force_light_title_bar(wizard)

    # Apply once more after the native window is fully initialized.
    QTimer.singleShot(100, lambda: force_light_title_bar(wizard))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
