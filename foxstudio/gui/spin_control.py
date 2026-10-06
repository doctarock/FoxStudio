"""Tiny always-on-top turntable spin control -- keep it next to JMStudio.

The turntable is a serial device (COM3), independent of the cameras, so it spins
happily while JMStudio owns the Fox cameras for scanning. Start/stop, adjust
speed and direction; that's it.

Launch: `foxstudio-turntable`  (or the desktop shortcut).
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QVBoxLayout, QWidget)

from ..turntable import Turntable, TurntableError


class SpinControl(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Turntable")
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.tt = None
        self.spinning = False

        v = QVBoxLayout(self)
        self.status = QLabel("connecting…")
        self.status.setStyleSheet("color:#888;")
        v.addWidget(self.status)

        srow = QHBoxLayout()
        srow.addWidget(QLabel("Speed"))
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(2, 60)
        self.speed.setValue(8)
        self.speed.valueChanged.connect(self._on_change)
        srow.addWidget(self.speed)
        self.speed_lbl = QLabel("8")
        self.speed_lbl.setMinimumWidth(24)
        srow.addWidget(self.speed_lbl)
        v.addLayout(srow)

        drow = QHBoxLayout()
        drow.addWidget(QLabel("Direction"))
        self.dir = QComboBox()
        self.dir.addItems(["Clockwise", "Counter-clockwise"])
        self.dir.currentIndexChanged.connect(self._on_change)
        drow.addWidget(self.dir, stretch=1)
        v.addLayout(drow)

        self.btn = QPushButton("Start")
        self.btn.setCheckable(True)
        self.btn.setMinimumHeight(52)
        self.btn.clicked.connect(self._toggle)
        v.addWidget(self.btn)

        self.resize(300, 170)
        self._connect()

    def _connect(self):
        try:
            self.tt = Turntable()
            self.tt.connect()
            self.status.setText(f"Connected on {self.tt.port}")
            self.status.setStyleSheet("color:#7c7;")
        except TurntableError as exc:
            self.status.setText(f"Not connected: {exc}")
            self.status.setStyleSheet("color:#c77;")
            self.btn.setEnabled(False)

    def _direction(self) -> int:
        return 1 if self.dir.currentIndex() == 0 else -1

    def _toggle(self, checked: bool):
        if not self.tt:
            return
        if checked:
            self.tt.spin(self.speed.value(), self._direction())
            self.spinning = True
            self.btn.setText("Stop")
            self.status.setText("Spinning")
        else:
            self.tt.stop_spin()
            self.spinning = False
            self.btn.setText("Start")
            self.status.setText("Stopped")

    def _on_change(self, *_):
        self.speed_lbl.setText(str(self.speed.value()))
        if self.spinning and self.tt:      # re-issue the move at the new speed/direction
            self.tt.stop_spin()
            self.tt.spin(self.speed.value(), self._direction())

    def closeEvent(self, event):
        if self.tt:
            try:
                self.tt.stop_spin()
                self.tt.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = SpinControl()
    win.show()
    if "--screenshot" in sys.argv:
        from PySide6.QtCore import QTimer
        out = sys.argv[sys.argv.index("--screenshot") + 1]
        QTimer.singleShot(1200, lambda: (win.grab().save(out), app.quit()))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
