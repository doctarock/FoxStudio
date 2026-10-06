"""Run pipeline calls off the GUI thread."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QThread, Signal


class Worker(QObject):
    finished = Signal(object)   # result
    failed = Signal(str)
    progress = Signal(str)      # log lines

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self):
        try:
            result = self._fn(self.progress.emit, *self._args, **self._kwargs)
            self.finished.emit(result)
        except Exception as exc:  # surfaced in the log pane, never crashes the app
            traceback.print_exc()
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class TaskRunner:
    """Owns one QThread at a time; serializes long-running actions."""

    def __init__(self, parent):
        self.parent = parent
        self._thread = None
        self._worker = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def run(self, fn, on_done, on_log, on_fail, *args, **kwargs) -> bool:
        if self.busy:
            return False
        self._thread = QThread(self.parent)
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(on_log)
        self._worker.finished.connect(on_done)
        self._worker.failed.connect(on_fail)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.start()
        return True
