"""Scan tab: live Fox camera preview, capture, calibration, reconstruction.

Workflow (no JMStudio needed):
  1. Connect cameras (close JMStudio first -- it locks the streams).
  2. One-time: print the checkerboard, collect ~10-15 calibration pairs,
     press Calibrate.
  3. Scan: each press of "Scan (capture + reconstruct)" grabs an A/B pair and
     turns it into a colored point cloud in the current project. Rotate the
     part between scans; "Fuse & Mesh" on the Project tab merges them.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .. import stereo
from ..capture.camera import find_fox_cameras, open_camera


def _to_pixmap(frame: np.ndarray, max_w: int = 480) -> QPixmap:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, _ = rgb.shape
    img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888)
    pm = QPixmap.fromImage(img)
    return pm.scaledToWidth(max_w, Qt.SmoothTransformation)


class FrameGrabber(QThread):
    """Continuously pulls A/B frame pairs off the GUI thread.

    Camera retrieve() can block for hundreds of ms on some backends; doing it
    here keeps the interface responsive no matter how a camera behaves.
    """
    pair_ready = Signal(object, object)  # (frame_a | None, frame_b | None)

    def __init__(self, cap_a, cap_b, parent=None):
        super().__init__(parent)
        self.cap_a = cap_a
        self.cap_b = cap_b
        self._running = True

    def run(self):
        while self._running:
            self.cap_a.grab()
            self.cap_b.grab()
            ok_a, fa = self.cap_a.retrieve()
            ok_b, fb = self.cap_b.retrieve()
            self.pair_ready.emit(fa if ok_a else None, fb if ok_b else None)
            self.msleep(50)
        self.cap_a.release()
        self.cap_b.release()

    def stop(self):
        self._running = False
        self.wait(3000)


class ScanPanel(QWidget):
    def __init__(self, main, parent=None):
        super().__init__(parent)
        self.main = main            # MainWindow (project access, logging, runner)
        self.grabber = None         # FrameGrabber thread while connected
        self.frames = (None, None)
        self.calib_pairs = []
        self.calibration = stereo.StereoCalibration.load()
        self.factory = None
        if self.calibration is None:
            # Prefer the factory calibration JMStudio cached for this unit -- it
            # beats a hand-rolled checkerboard and needs no setup.
            try:
                from .. import fox_calibration
                fox = fox_calibration.load()
                if fox is not None:
                    self.factory = fox
                    self.calibration = fox.as_stereo()
            except Exception:
                self.factory = None
        self.reconstructor = None

        root = QVBoxLayout(self)

        # camera previews
        cams = QHBoxLayout()
        self.preview_a = QLabel("Camera A\n(not connected)")
        self.preview_b = QLabel("Camera B\n(not connected)")
        for lbl in (self.preview_a, self.preview_b):
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumSize(480, 270)
            lbl.setStyleSheet("background:#111; color:#888; border:1px solid #333;")
            cams.addWidget(lbl)
        root.addLayout(cams)

        # connection + scan controls
        row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect Fox cameras")
        self.btn_connect.clicked.connect(self.toggle_cameras)
        row.addWidget(self.btn_connect)
        self.btn_scan = QPushButton("Scan  (capture + reconstruct)")
        self.btn_scan.setEnabled(False)
        self.btn_scan.clicked.connect(self.scan_once)
        row.addWidget(self.btn_scan)
        self.btn_capture = QPushButton("Capture raw pair only")
        self.btn_capture.setEnabled(False)
        self.btn_capture.clicked.connect(self.capture_raw)
        row.addWidget(self.btn_capture)
        root.addLayout(row)

        # calibration controls
        cal = QGroupBox("Stereo calibration (one-time)")
        cal_row = QHBoxLayout(cal)
        self.btn_board = QPushButton("Save printable board...")
        self.btn_board.clicked.connect(self.save_board)
        cal_row.addWidget(self.btn_board)
        self.btn_grab_cal = QPushButton("Grab calibration pair")
        self.btn_grab_cal.setEnabled(False)
        self.btn_grab_cal.clicked.connect(self.grab_calibration_pair)
        cal_row.addWidget(self.btn_grab_cal)
        self.spin_needed = QSpinBox()
        self.spin_needed.setRange(8, 40)
        self.spin_needed.setValue(12)
        self.spin_needed.setPrefix("target pairs: ")
        cal_row.addWidget(self.spin_needed)
        self.btn_calibrate = QPushButton("Calibrate")
        self.btn_calibrate.setEnabled(False)
        self.btn_calibrate.clicked.connect(self.run_calibration)
        cal_row.addWidget(self.btn_calibrate)
        self.lbl_calib = QLabel()
        cal_row.addWidget(self.lbl_calib, stretch=1)
        root.addWidget(cal)

        # laser scanning (Ciclop turntable + line lasers)
        laser = QGroupBox("Laser scan  (Ciclop turntable + lasers)")
        lrow = QHBoxLayout(laser)
        self.btn_laser_aim = QPushButton("Aim lasers...")
        self.btn_laser_aim.clicked.connect(self.act_laser_aim)
        lrow.addWidget(self.btn_laser_aim)
        self.btn_axis = QPushButton("Calibrate turntable axis")
        self.btn_axis.clicked.connect(self.act_calibrate_axis)
        lrow.addWidget(self.btn_axis)
        self.spin_steps = QSpinBox()
        self.spin_steps.setRange(24, 400)
        self.spin_steps.setValue(200)
        self.spin_steps.setPrefix("steps: ")
        lrow.addWidget(self.spin_steps)
        self.btn_laser_scan = QPushButton("Laser scan")
        self.btn_laser_scan.clicked.connect(self.act_laser_scan)
        lrow.addWidget(self.btn_laser_scan)
        root.addWidget(laser)

        note = QLabel(
            "Native passive stereo needs surface texture. For textureless parts, use "
            "Laser scan: a Ciclop turntable + line lasers draw a stripe both cameras see, "
            "reconstructed with the factory calibration. Aim first (clean line in both "
            "cameras), calibrate the turntable axis once (checkerboard on the platform), "
            "then Laser scan. Or scan in JMStudio and import the export.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#999;")
        root.addWidget(note)
        self._update_calib_label()

    # -- cameras -----------------------------------------------------------
    @property
    def connected(self) -> bool:
        return self.grabber is not None and self.grabber.isRunning()

    def toggle_cameras(self):
        if self.connected:
            self.grabber.stop()
            self.grabber = None
            self.frames = (None, None)
            self.btn_connect.setText("Connect Fox cameras")
            for b in (self.btn_scan, self.btn_capture, self.btn_grab_cal):
                b.setEnabled(False)
            self.preview_a.setText("Camera A\n(not connected)")
            self.preview_b.setText("Camera B\n(not connected)")
            return
        refs = find_fox_cameras()
        if len(refs) < 2:
            QMessageBox.warning(
                self, "Fox not found",
                f"Found {len(refs)} of 2 Fox cameras.\n\nPlug in the Fox (press its "
                "power button if it went to sleep) and close JMStudio (it holds "
                "the cameras exclusively).")
            return
        try:
            caps = (open_camera(refs[0]), open_camera(refs[1]))
        except IOError as exc:
            QMessageBox.warning(self, "Camera error", str(exc))
            return
        self.grabber = FrameGrabber(*caps, parent=self)
        self.grabber.pair_ready.connect(self._on_pair)
        self.grabber.start()
        self.btn_connect.setText("Disconnect")
        self.btn_capture.setEnabled(True)
        self.btn_grab_cal.setEnabled(True)
        self.btn_scan.setEnabled(self.calibration is not None)

    def _on_pair(self, fa, fb):
        last_a, last_b = self.frames
        self.frames = (fa if fa is not None else last_a,
                       fb if fb is not None else last_b)
        if fa is not None:
            self.preview_a.setPixmap(_to_pixmap(fa))
        if fb is not None:
            self.preview_b.setPixmap(_to_pixmap(fb))

    def closeEvent(self, event):
        if self.connected:
            self.grabber.stop()
        super().closeEvent(event)

    # -- calibration -------------------------------------------------------
    def save_board(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "Save calibration board", "fox_calibration_board.png", "PNG (*.png)")
        if path:
            stereo.make_checkerboard_pdf(Path(path))
            self.main.log(f"Calibration board saved: {path} -- print at 100% scale "
                          f"({stereo.BOARD_COLS}x{stereo.BOARD_ROWS} inner corners, "
                          f"{stereo.SQUARE_MM:.0f} mm squares)")

    def grab_calibration_pair(self):
        fa, fb = self.frames
        if fa is None:
            return
        ga = cv2.cvtColor(fa, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(fb, cv2.COLOR_BGR2GRAY)
        ca, cb = stereo.find_board(ga), stereo.find_board(gb)
        if ca is None or cb is None:
            missing = "A and B" if ca is None and cb is None else ("A" if ca is None else "B")
            self.main.log(f"Board NOT visible in camera {missing} -- adjust and try again.")
            return
        self.calib_pairs.append((fa.copy(), fb.copy()))
        n = len(self.calib_pairs)
        self.main.log(f"Calibration pair {n}/{self.spin_needed.value()} captured "
                      "(move the board to a new pose).")
        self.btn_calibrate.setEnabled(n >= 8)

    def run_calibration(self):
        pairs = list(self.calib_pairs)

        def job(log):
            log(f"Calibrating from {len(pairs)} pairs ...")
            calib = stereo.calibrate(pairs)
            calib.save()
            return calib

        def done(calib):
            self.calibration = calib
            self.reconstructor = None
            self.calib_pairs.clear()
            self._update_calib_label()
            self.btn_scan.setEnabled(self.connected)
            self.main.log(f"Calibration complete: RMS reprojection error "
                          f"{calib.rms:.3f} px (saved to {stereo.CALIB_FILE})")

        self.main.run_task(job, done)

    # -- laser scanning ----------------------------------------------------
    def _connect_turntable(self):
        from ..turntable import Turntable, TurntableError
        try:
            tt = Turntable()
            banner = tt.connect()
            self.main.log(f"Turntable connected on {tt.port}: {banner or '(no banner)'}")
            return tt
        except TurntableError as exc:
            QMessageBox.warning(self, "Turntable", str(exc))
            return None

    def act_laser_aim(self):
        """Open the interactive aim window (separate OpenCV window)."""
        if self.connected:              # free the cameras for the aim tool (DSHOW)
            self.toggle_cameras()
        tt = self._connect_turntable()
        if tt is None:
            return
        from ..laserscan import laser_aim
        self.main.log("Laser aim: adjust for a clean green line in BOTH cameras, then "
                      "press q in the aim window. (This main window pauses meanwhile.)")
        try:
            laser_aim(tt)
        except Exception as exc:        # noqa: surfaced to the log, never crash the app
            self.main.log(f"aim error: {exc}")
        finally:
            tt.disconnect()

    def act_calibrate_axis(self):
        if self.connected:
            self.toggle_cameras()
        tt = self._connect_turntable()
        if tt is None:
            return
        views = 12

        def job(log):
            import time
            from .. import fox_calibration
            from ..stereo import Reconstructor
            from ..laserscan import calibrate_axis_from_checkerboard
            from ..capture.camera import find_fox_cameras
            fox = fox_calibration.load()
            if fox is None:
                raise RuntimeError("No factory calibration.")
            recon = Reconstructor(fox.as_stereo())
            refs = find_fox_cameras()
            cap = cv2.VideoCapture(refs[0].index, cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            poses = []
            step = 360.0 / views
            log("Place the checkerboard flat on the turntable...")
            tt.reset_origin()
            for k in range(views):
                time.sleep(0.4)
                for _ in range(4):
                    cap.grab()
                ok, frame = cap.retrieve()
                rect = cv2.remap(frame, recon.map1[0], recon.map1[1], cv2.INTER_LINEAR)
                poses.append((k * step, rect))
                log(f"  view {k + 1}/{views}")
                tt.rotate(step)
            cap.release()
            axis = calibrate_axis_from_checkerboard(recon, poses)
            axis.save()
            return axis

        def done(axis):
            tt.disconnect()
            if axis is not None:
                self.main.log(f"Turntable axis saved: point {axis.point.round(1)}, "
                              f"dir {axis.direction.round(3)}")
        self.main.run_task(job, done)

    def act_laser_scan(self):
        proj = self.main.current_project()
        if proj is None:
            QMessageBox.information(self, "No project", "Create or select a project first.")
            return
        if self.connected:
            self.toggle_cameras()
        tt = self._connect_turntable()
        if tt is None:
            return
        steps = self.spin_steps.value()

        def job(log):
            from ..laserscan import laser_scan
            try:
                return laser_scan(proj, tt, steps=steps, log=log)
            finally:
                tt.disconnect()

        def done(cloud):
            if cloud is not None and len(cloud.points):
                self.main.show_in_viewer(cloud, f"laser scan - {len(cloud.points):,} points")
            self.main.refresh_projects(keep_selection=True)
        self.main.run_task(job, done)

    def _update_calib_label(self):
        if self.factory is not None:
            self.lbl_calib.setText(
                f"factory calibration ({self.factory.serial}, "
                f"baseline {self.factory.baseline_mm:.1f} mm) -- no setup needed")
            self.lbl_calib.setStyleSheet("color:#7c7;")
        elif self.calibration:
            self.lbl_calib.setText(f"calibrated (RMS {self.calibration.rms:.2f} px)")
            self.lbl_calib.setStyleSheet("color:#7c7;")
        else:
            self.lbl_calib.setText("not calibrated -- native scanning disabled")
            self.lbl_calib.setStyleSheet("color:#c77;")

    # -- scanning ----------------------------------------------------------
    def _require_project(self):
        proj = self.main.current_project()
        if proj is None:
            QMessageBox.information(self, "No project",
                                    "Create or select a project first (left panel).")
        return proj

    def capture_raw(self):
        proj = self._require_project()
        if proj is None or self.frames[0] is None:
            return
        fa, fb = self.frames
        session = proj.path / "captures" / datetime.now().strftime("native-%Y%m%d")
        session.mkdir(parents=True, exist_ok=True)
        n = len(list(session.glob("pair_*_A.png")))
        cv2.imwrite(str(session / f"pair_{n:04d}_A.png"), fa)
        cv2.imwrite(str(session / f"pair_{n:04d}_B.png"), fb)
        self.main.log(f"Raw pair {n} saved to {session}")

    def scan_once(self):
        proj = self._require_project()
        if proj is None or self.frames[0] is None or self.calibration is None:
            return
        fa, fb = self.frames[0].copy(), self.frames[1].copy()

        def job(log):
            if self.reconstructor is None:
                self.reconstructor = stereo.Reconstructor(self.calibration)
            log("Reconstructing point cloud from stereo pair ...")
            pcd = self.reconstructor.reconstruct(fa, fb)
            if len(pcd.points) < 1000:
                raise ValueError(
                    f"Only {len(pcd.points)} points recovered -- not enough texture/"
                    "overlap. Improve lighting or add surface detail.")
            from .. import meshio
            n = len(list((proj.path / "raw").glob("native_scan_*.ply")))
            out = proj.path / "raw" / f"native_scan_{n:03d}.ply"
            meshio.save(pcd, out)
            proj.meta["files"].append({
                "name": out.name, "role": "raw",
                "imported": datetime.now().isoformat(timespec="seconds"),
                "source": "fox native stereo capture",
            })
            proj.save()
            return out, pcd

        def done(result):
            out, pcd = result
            self.main.log(f"Native scan saved: {out.name} ({len(pcd.points):,} points). "
                          "Rotate the part and scan again; then Fuse & Mesh.")
            self.main.show_in_viewer(pcd, f"{out.name} - {len(pcd.points):,} points")
            self.main.refresh_projects(keep_selection=True)

        self.main.run_task(job, done)
