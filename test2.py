import sys
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QCheckBox,
    QGraphicsDropShadowEffect
)


class ModernLoginWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        # 1. Window Setup
        self.setWindowTitle("Sign In")
        self.setFixedSize(450, 520)  # Slightly reduced height since footer/links are gone
        # Frameless window with transparent background for custom corner radii
        self.setWindowFlags(Qt.WindowFlags(Qt.FramelessWindowHint))
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 2. Main Background/Container
        self.central_widget = QWidget(self)
        self.central_widget.setObjectName("CentralWidget")
        self.setCentralWidget(self.central_widget)

        # 3. Main Layout
        self.main_layout = QVBoxLayout(self.central_widget)
        self.main_layout.setContentsMargins(40, 50, 40, 50)
        self.main_layout.setSpacing(0)

        # --- UI ELEMENTS ---

        # Top Header Layout (Title + Close Button)
        header_layout = QHBoxLayout()

        self.title_label = QLabel("Welcome Back")
        self.title_label.setObjectName("TitleLabel")
        header_layout.addWidget(self.title_label)

        # Subtle custom close button
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("CloseButton")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.clicked.connect(self.close)
        header_layout.addWidget(self.close_btn, alignment=Qt.AlignTop | Qt.AlignRight)

        self.main_layout.addLayout(header_layout)

        # Subtitle
        self.subtitle_label = QLabel("Please enter your details to sign in.")
        self.subtitle_label.setObjectName("SubtitleLabel")
        self.main_layout.addWidget(self.subtitle_label)
        self.main_layout.addSpacing(40)

        # Input Form Labels & LineEdits - Changed to USERNAME
        self.username_label = QLabel("USERNAME")
        self.username_label.setObjectName("FormLabel")
        self.main_layout.addWidget(self.username_label)

        self.username_input = QLineEdit()
        self.username_input.setObjectName("InputField")
        self.username_input.setPlaceholderText("Enter your username")
        self.main_layout.addWidget(self.username_input)
        self.main_layout.addSpacing(20)

        # Password
        self.password_label = QLabel("PASSWORD")
        self.password_label.setObjectName("FormLabel")
        self.main_layout.addWidget(self.password_label)

        self.password_input = QLineEdit()
        self.password_input.setObjectName("InputField")
        self.password_input.setEchoMode(QLineEdit.Password)
        self.password_input.setPlaceholderText("••••••••••••")
        self.main_layout.addWidget(self.password_input)
        self.main_layout.addSpacing(20)

        # Options (Remember Me Only - Forgot Password Removed)
        options_layout = QHBoxLayout()
        self.remember_me = QCheckBox("Remember me")
        self.remember_me.setObjectName("RememberMeCheckbox")
        options_layout.addWidget(self.remember_me)
        self.main_layout.addLayout(options_layout)
        self.main_layout.addSpacing(40)

        # Sign In Button
        self.login_btn = QPushButton("Sign In")
        self.login_btn.setObjectName("LoginButton")
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.main_layout.addWidget(self.login_btn)

        self.main_layout.addStretch()

        # 4. Global Stylesheet Application (Modern Dark Aesthetic)
        self.apply_styles()

        # 5. Drop Shadow Effect for the "Card" feel
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setXOffset(0)
        shadow.setYOffset(10)
        shadow.setColor(QColor(0, 0, 0, 160))
        self.central_widget.setGraphicsEffect(shadow)

        # Draggable window behavior
        self._drag_position = None

    def apply_styles(self):
        qss = """
            /* Main Window Card styling */
            QWidget#CentralWidget {
                background-color: #0F172A; /* Slate 900 */
                border: 1px solid #1E293B; /* Slate 800 */
                border-radius: 16px;
            }

            /* Text elements */
            QLabel#TitleLabel {
                color: #F8FAFC;
                font-size: 26px;
                font-weight: bold;
                font-family: 'Segoe UI', Arial, sans-serif;
            }
            QLabel#SubtitleLabel {
                color: #94A3B8;
                font-size: 14px;
                font-family: 'Segoe UI', sans-serif;
                margin-top: 4px;
            }
            QLabel#FormLabel {
                color: #64748B;
                font-size: 11px;
                font-weight: bold;
                letter-spacing: 1px;
                margin-bottom: 6px;
            }

            /* Text inputs */
            QLineEdit#InputField {
                background-color: #1E293B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 12px 16px;
                font-size: 14px;
            }
            QLineEdit#InputField:focus {
                border: 1px solid #38BDF8; /* Sky 400 highlight */
                background-color: #0F172A;
            }

            /* Call-to-action Button */
            QPushButton#LoginButton {
                background-color: #38BDF8; /* Sky 400 */
                color: #0F172A;
                font-size: 15px;
                font-weight: bold;
                border: none;
                border-radius: 8px;
                padding: 14px;
            }
            QPushButton#LoginButton:hover {
                background-color: #7DD3FC; /* Sky 300 */
            }
            QPushButton#LoginButton:pressed {
                background-color: #0EA5E9; /* Sky 500 */
            }

            /* Close Button */
            QPushButton#CloseButton {
                color: #64748B;
                background-color: transparent;
                border: none;
                border-radius: 14px;
                font-size: 14px;
            }
            QPushButton#CloseButton:hover {
                color: #F8FAFC;
                background-color: #EF4444; /* Soft Red */
            }

            /* Checkbox */
            QCheckBox#RememberMeCheckbox {
                color: #94A3B8;
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox#RememberMeCheckbox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #334155;
                border-radius: 4px;
                background: #1E293B;
            }
            QCheckBox#RememberMeCheckbox::indicator:checked {
                background: #38BDF8;
                border-color: #38BDF8;
            }
        """
        self.setStyleSheet(qss)

    # Window drag physics since we stripped the native window frames
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton and self._drag_position is not None:
            self.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_position = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = ModernLoginWindow()
    window.show()
    sys.exit(app.exec())