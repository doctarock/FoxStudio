"""Native Fox scanning: stereo calibration and 3D reconstruction.

The Fox exposes its two cameras as plain UVC devices but computes depth in
JMStudio (no depth on the wire). This module makes scanning work without
JMStudio via classic two-view stereo:

  1. calibrate once with a printed checkerboard shown to both cameras
     (intrinsics + extrinsics + rectification maps, cached on disk)
  2. rectify each captured A/B pair
  3. StereoSGBM disparity -> reproject to a metric point cloud

Passive stereo needs surface texture; matte featureless parts reconstruct
poorly until the Fox's projector protocol is reverse engineered (see
docs/FOX_USB_FINDINGS.md). A light dusting / pencil speckle on the part or
projecting any pattern with a torch/laser helps a lot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import open3d as o3d

from .config import CONFIG_DIR

CALIB_FILE = CONFIG_DIR / "fox_stereo_calib.json"

# 9x6 inner corners, 20 mm squares -- print references/checkerboard A4 at 100%
BOARD_COLS = 9
BOARD_ROWS = 6
SQUARE_MM = 20.0


@dataclass
class StereoCalibration:
    image_size: tuple  # (w, h)
    K1: np.ndarray
    D1: np.ndarray
    K2: np.ndarray
    D2: np.ndarray
    R: np.ndarray
    T: np.ndarray
    rms: float

    def swapped(self) -> "StereoCalibration":
        """Same rig with the two cameras' roles exchanged (cam2 as reference).
        Used to resolve which physical camera is the calibration's left camera."""
        R = np.asarray(self.R)
        T = np.asarray(self.T).reshape(3)
        return StereoCalibration(
            image_size=self.image_size,
            K1=self.K2, D1=self.D2, K2=self.K1, D2=self.D1,
            R=R.T, T=(-R.T @ T).reshape(3, 1), rms=self.rms)

    def save(self) -> Path:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "image_size": list(self.image_size),
            "K1": self.K1.tolist(), "D1": self.D1.tolist(),
            "K2": self.K2.tolist(), "D2": self.D2.tolist(),
            "R": self.R.tolist(), "T": self.T.tolist(),
            "rms": self.rms,
            "board": [BOARD_COLS, BOARD_ROWS, SQUARE_MM],
        }
        CALIB_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return CALIB_FILE

    @classmethod
    def load(cls) -> Optional["StereoCalibration"]:
        if not CALIB_FILE.exists():
            return None
        d = json.loads(CALIB_FILE.read_text(encoding="utf-8-sig"))
        return cls(
            image_size=tuple(d["image_size"]),
            K1=np.array(d["K1"]), D1=np.array(d["D1"]),
            K2=np.array(d["K2"]), D2=np.array(d["D2"]),
            R=np.array(d["R"]), T=np.array(d["T"]),
            rms=float(d["rms"]),
        )


def find_board(gray: np.ndarray):
    """Checkerboard corners or None."""
    flags = cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    ok, corners = cv2.findChessboardCorners(gray, (BOARD_COLS, BOARD_ROWS), flags)
    if not ok:
        return None
    return cv2.cornerSubPix(
        gray, corners, (11, 11), (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001))


def calibrate(pairs: list[tuple[np.ndarray, np.ndarray]]) -> StereoCalibration:
    """Stereo-calibrate from BGR image pairs where both views see the board.

    Raises ValueError when too few usable pairs are supplied.
    """
    objp = np.zeros((BOARD_COLS * BOARD_ROWS, 3), np.float32)
    objp[:, :2] = np.mgrid[0:BOARD_COLS, 0:BOARD_ROWS].T.reshape(-1, 2) * SQUARE_MM

    obj_points, pts1, pts2 = [], [], []
    size = None
    for img_a, img_b in pairs:
        g1 = cv2.cvtColor(img_a, cv2.COLOR_BGR2GRAY)
        g2 = cv2.cvtColor(img_b, cv2.COLOR_BGR2GRAY)
        size = (g1.shape[1], g1.shape[0])
        c1, c2 = find_board(g1), find_board(g2)
        if c1 is not None and c2 is not None:
            obj_points.append(objp)
            pts1.append(c1)
            pts2.append(c2)

    if len(obj_points) < 8:
        raise ValueError(
            f"Only {len(obj_points)} pair(s) show the full checkerboard in BOTH "
            "cameras; need at least 8. Move the board around the overlap zone "
            "(different angles and distances) and capture more.")

    _, K1, D1, _, _ = cv2.calibrateCamera(obj_points, pts1, size, None, None)
    _, K2, D2, _, _ = cv2.calibrateCamera(obj_points, pts2, size, None, None)
    rms, K1, D1, K2, D2, R, T, _, _ = cv2.stereoCalibrate(
        obj_points, pts1, pts2, K1, D1, K2, D2, size,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-5),
        flags=cv2.CALIB_FIX_INTRINSIC)
    return StereoCalibration(size, K1, D1, K2, D2, R, T, float(rms))


class Reconstructor:
    """Rectification + SGBM depth for calibrated pairs."""

    def __init__(self, calib: StereoCalibration):
        self.calib = calib
        size = tuple(int(v) for v in calib.image_size)
        R1, R2, P1, P2, Q, roi1, roi2 = cv2.stereoRectify(
            calib.K1, calib.D1, calib.K2, calib.D2, size, calib.R, calib.T,
            alpha=0)
        self.Q = Q
        self.map1 = cv2.initUndistortRectifyMap(calib.K1, calib.D1, R1, P1, size, cv2.CV_16SC2)
        self.map2 = cv2.initUndistortRectifyMap(calib.K2, calib.D2, R2, P2, size, cv2.CV_16SC2)
        self.matcher = cv2.StereoSGBM_create(
            minDisparity=0,
            numDisparities=16 * 12,
            blockSize=5,
            P1=8 * 3 * 5 ** 2,
            P2=32 * 3 * 5 ** 2,
            uniquenessRatio=8,
            speckleWindowSize=120,
            speckleRange=2,
            disp12MaxDiff=1,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY)

    def reconstruct(
        self,
        img_a: np.ndarray,
        img_b: np.ndarray,
        z_min: float = 80.0,
        z_max: float = 600.0,
    ) -> o3d.geometry.PointCloud:
        """One rectified pair -> colored metric point cloud (mm), camera frame."""
        ra = cv2.remap(img_a, *self.map1, cv2.INTER_LINEAR)
        rb = cv2.remap(img_b, *self.map2, cv2.INTER_LINEAR)
        disp = self.matcher.compute(
            cv2.cvtColor(ra, cv2.COLOR_BGR2GRAY),
            cv2.cvtColor(rb, cv2.COLOR_BGR2GRAY)).astype(np.float32) / 16.0
        pts = cv2.reprojectImageTo3D(disp, self.Q)
        mask = (disp > 0) & np.isfinite(pts).all(axis=2)
        mask &= (pts[:, :, 2] > z_min) & (pts[:, :, 2] < z_max)
        xyz = pts[mask]
        rgb = cv2.cvtColor(ra, cv2.COLOR_BGR2RGB)[mask] / 255.0
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(xyz.astype(np.float64))
        pcd.colors = o3d.utility.Vector3dVector(rgb.astype(np.float64))
        if len(pcd.points) > 500:
            pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
        return pcd


def make_checkerboard_pdf(path: Path) -> Path:
    """Write a printable A4 checkerboard PNG (print at 100% scale)."""
    # A4 @ 150 dpi = 1240x1754 px; 20 mm squares = 118.1 px
    px_per_mm = 150 / 25.4
    sq = int(round(SQUARE_MM * px_per_mm))
    cols, rows = BOARD_COLS + 1, BOARD_ROWS + 1
    board = np.fromfunction(
        lambda y, x: ((x // sq + y // sq) % 2) * 255, (rows * sq, cols * sq)).astype(np.uint8)
    page = np.full((1754, 1240), 255, np.uint8)
    y0 = (page.shape[0] - board.shape[0]) // 2
    x0 = (page.shape[1] - board.shape[1]) // 2
    page[y0:y0 + board.shape[0], x0:x0 + board.shape[1]] = board
    cv2.putText(page, f"FoxStudio calibration board {BOARD_COLS}x{BOARD_ROWS} @ {SQUARE_MM:.0f}mm - print at 100%",
                (x0, y0 - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), page)
    return path
