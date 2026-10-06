"""Entry point: `foxstudio-desktop` or `python -m foxstudio.gui.app`.

`--screenshot <path>` renders the window, saves a PNG, and exits (self-test).
"""
from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication
    from .mainwindow import MainWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()

    if "--screenshot" in sys.argv:
        from PySide6.QtCore import QTimer
        out = sys.argv[sys.argv.index("--screenshot") + 1]

        def snap():
            win.grab().save(out)
            app.quit()

        QTimer.singleShot(2500, snap)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
