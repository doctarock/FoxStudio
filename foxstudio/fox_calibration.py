"""Parse the 3DMakerpro Fox factory calibration that JMStudio caches on disk.

JMStudio writes a per-unit file  <install>/download/calib_<SERIAL>.txt  when the
scanner is first used. It is plain text and contains the full structured-light
calibration, so FoxStudio can scan natively with factory-grade calibration and
**no checkerboard step**.

File format (verified on unit JMM8004902, "Factory-15", SoftVersion 3.0.0.1116):
    line 1   : <img_w> <img_h>
    line 2   : sensor/active area, mm (informational)
    line 3-4 : projector working-range / index hints
    line 5   : camera 1 (reference):  fx fy cx cy  k1 k2 p1 p2 k3  rvec(3) tvec(3)
    line 6   : camera 2:              same layout, extrinsics relative to cam 1
    line 7   : projector as a camera: same layout, extrinsics relative to cam 1
    line 8   : N   (projector column LUT length)
    next N   : <idx> <coeff> <position_mm>   phase/column -> ray LUT
    line     : M   (pattern row count)
    next M   : 6 integers in {1,2,3,4}   fixed projected stripe code (De Bruijn)
    footer   : ***DevID:...***CalibrateDate:...***Type:...***SoftVersion:...***

Distances are millimetres. rvec is a Rodrigues vector (radians).
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import CONFIG_DIR
from .stereo import StereoCalibration

# where FoxStudio keeps its own copy so it never depends on the JMStudio path
FACTORY_DIR = CONFIG_DIR / "factory_calib"

JMSTUDIO_GLOBS = [
    r"C:\Program Files (x86)\JMStudio\*\download",
    r"C:\Program Files\JMStudio\*\download",
]


@dataclass
class FoxCalibration:
    serial: str
    image_size: tuple            # (w, h)
    K1: np.ndarray
    D1: np.ndarray
    K2: np.ndarray
    D2: np.ndarray
    R: np.ndarray                # cam1 -> cam2 rotation
    T: np.ndarray                # cam1 -> cam2 translation (mm)
    proj_K: np.ndarray
    proj_D: np.ndarray
    proj_R: np.ndarray           # cam1 -> projector
    proj_T: np.ndarray
    lut: np.ndarray              # (N,3): index, coeff, position_mm
    pattern: np.ndarray          # (M,6) ints in 1..4
    meta: dict

    # camera baseline in mm
    @property
    def baseline_mm(self) -> float:
        return float(np.linalg.norm(self.T))

    def as_stereo(self) -> StereoCalibration:
        """Adapt to the StereoCalibration the SGBM Reconstructor consumes."""
        return StereoCalibration(
            image_size=self.image_size,
            K1=self.K1, D1=self.D1, K2=self.K2, D2=self.D2,
            R=self.R, T=self.T.reshape(3, 1),
            rms=float(self.meta.get("rms", 0.0)))


def _parse_cam(line: str):
    v = list(map(float, line.split()))
    fx, fy, cx, cy = v[0:4]
    K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
    D = np.array(v[4:9], dtype=np.float64)          # k1 k2 p1 p2 k3
    rvec = np.array(v[9:12], dtype=np.float64)
    tvec = np.array(v[12:15], dtype=np.float64)
    R, _ = cv2.Rodrigues(rvec)
    return K, D, R, tvec


def parse_file(path: Path) -> FoxCalibration:
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    w, h = map(int, lines[0].split())
    K1, D1, _, _ = _parse_cam(lines[4])
    K2, D2, R2, t2 = _parse_cam(lines[5])
    Kp, Dp, Rp, tp = _parse_cam(lines[6])

    n_lut = int(lines[7])
    lut = np.array([list(map(float, lines[8 + i].split())) for i in range(n_lut)])

    p0 = 8 + n_lut
    m = int(lines[p0])
    pattern = np.array([list(map(int, lines[p0 + 1 + i].split())) for i in range(m)])

    footer = lines[-1] if lines[-1].startswith("***") else ""
    meta = dict(re.findall(r"\*\*\*([^:]+):([^*]+)", footer))
    return FoxCalibration(
        serial=meta.get("DevID", Path(path).stem.replace("calib_", "")),
        image_size=(w, h),
        K1=K1, D1=D1, K2=K2, D2=D2, R=R2, T=t2,
        proj_K=Kp, proj_D=Dp, proj_R=Rp, proj_T=tp,
        lut=lut, pattern=pattern, meta=meta)


def find_jmstudio_calibrations() -> list[Path]:
    found = []
    for pattern in JMSTUDIO_GLOBS:
        base = Path(pattern.split("*")[0])
        if not base.exists():
            continue
        for download in Path(base).glob(pattern[len(str(base)):].lstrip("\\/")):
            found.extend(download.glob("calib_*.txt"))
    return sorted(set(found))


def import_from_jmstudio(serial: Optional[str] = None) -> Optional[FoxCalibration]:
    """Copy the JMStudio factory calib into FoxStudio's store and return it."""
    candidates = find_jmstudio_calibrations()
    if serial:
        candidates = [c for c in candidates if serial in c.name]
    if not candidates:
        return None
    src = candidates[0]
    FACTORY_DIR.mkdir(parents=True, exist_ok=True)
    dest = FACTORY_DIR / src.name
    shutil.copy2(src, dest)
    return parse_file(dest)


def load(serial: Optional[str] = None) -> Optional[FoxCalibration]:
    """Return a stored factory calibration, importing from JMStudio if needed."""
    if FACTORY_DIR.exists():
        stored = sorted(FACTORY_DIR.glob("calib_*.txt"))
        if serial:
            stored = [s for s in stored if serial in s.name]
        if stored:
            return parse_file(stored[0])
    return import_from_jmstudio(serial)
