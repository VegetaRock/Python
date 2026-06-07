# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'pdfEditorArhflp.ui'
##
## Created by: Qt User Interface Compiler version 6.8.0
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QApplication, QGraphicsView, QGridLayout, QHBoxLayout,
    QMainWindow, QPushButton, QSizePolicy, QSpacerItem,
    QSpinBox, QStatusBar, QVBoxLayout, QWidget)

class Ui_MainWindow(object):
    def setupUi(self, MainWindow):
        if not MainWindow.objectName():
            MainWindow.setObjectName(u"MainWindow")
        MainWindow.resize(1245, 756)
        self.centralwidget = QWidget(MainWindow)
        self.centralwidget.setObjectName(u"centralwidget")
        self.verticalLayout = QVBoxLayout(self.centralwidget)
        self.verticalLayout.setObjectName(u"verticalLayout")
        self.toolBar = QWidget(self.centralwidget)
        self.toolBar.setObjectName(u"toolBar")
        self.horizontalLayout = QHBoxLayout(self.toolBar)
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.btnCircle = QPushButton(self.toolBar)
        self.btnCircle.setObjectName(u"btnCircle")

        self.horizontalLayout.addWidget(self.btnCircle)

        self.btnArrow = QPushButton(self.toolBar)
        self.btnArrow.setObjectName(u"btnArrow")

        self.horizontalLayout.addWidget(self.btnArrow)

        self.btnText = QPushButton(self.toolBar)
        self.btnText.setObjectName(u"btnText")

        self.horizontalLayout.addWidget(self.btnText)

        self.spinBoxTextSize = QSpinBox(self.toolBar)
        self.spinBoxTextSize.setObjectName(u"spinBoxTextSize")

        self.horizontalLayout.addWidget(self.spinBoxTextSize)

        self.horizontalSpacer = QSpacerItem(1125, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.horizontalLayout.addItem(self.horizontalSpacer)


        self.verticalLayout.addWidget(self.toolBar)

        self.viewerWindow = QWidget(self.centralwidget)
        self.viewerWindow.setObjectName(u"viewerWindow")
        self.gridLayout = QGridLayout(self.viewerWindow)
        self.gridLayout.setObjectName(u"gridLayout")
        self.graphicsView = QGraphicsView(self.viewerWindow)
        self.graphicsView.setObjectName(u"graphicsView")

        self.gridLayout.addWidget(self.graphicsView, 0, 0, 1, 1)


        self.verticalLayout.addWidget(self.viewerWindow)

        self.btnBar = QWidget(self.centralwidget)
        self.btnBar.setObjectName(u"btnBar")
        self.horizontalLayout_2 = QHBoxLayout(self.btnBar)
        self.horizontalLayout_2.setObjectName(u"horizontalLayout_2")
        self.btnCorrection = QPushButton(self.btnBar)
        self.btnCorrection.setObjectName(u"btnCorrection")

        self.horizontalLayout_2.addWidget(self.btnCorrection)

        self.horizontalSpacer_2 = QSpacerItem(963, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.horizontalLayout_2.addItem(self.horizontalSpacer_2)

        self.btnAppr = QPushButton(self.btnBar)
        self.btnAppr.setObjectName(u"btnAppr")

        self.horizontalLayout_2.addWidget(self.btnAppr)

        self.btnCancel = QPushButton(self.btnBar)
        self.btnCancel.setObjectName(u"btnCancel")

        self.horizontalLayout_2.addWidget(self.btnCancel)


        self.verticalLayout.addWidget(self.btnBar)

        self.verticalLayout.setStretch(0, 5)
        self.verticalLayout.setStretch(1, 90)
        self.verticalLayout.setStretch(2, 5)
        MainWindow.setCentralWidget(self.centralwidget)
        self.statusbar = QStatusBar(MainWindow)
        self.statusbar.setObjectName(u"statusbar")
        MainWindow.setStatusBar(self.statusbar)

        self.retranslateUi(MainWindow)

        QMetaObject.connectSlotsByName(MainWindow)
    # setupUi

    def retranslateUi(self, MainWindow):
        MainWindow.setWindowTitle(QCoreApplication.translate("MainWindow", u"MainWindow", None))
        self.btnCircle.setText(QCoreApplication.translate("MainWindow", u"Circle", None))
        self.btnArrow.setText(QCoreApplication.translate("MainWindow", u"Arrow", None))
        self.btnText.setText(QCoreApplication.translate("MainWindow", u"Text", None))
        self.btnCorrection.setText(QCoreApplication.translate("MainWindow", u"Correction", None))
        self.btnAppr.setText(QCoreApplication.translate("MainWindow", u"Approve", None))
        self.btnCancel.setText(QCoreApplication.translate("MainWindow", u"Cancel", None))
    # retranslateUi

