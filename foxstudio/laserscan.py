"""Laser-line stereo scanning for the Fox + a Ciclop/Horus turntable & lasers.

This is an alternative to the Fox's own (un-crackable) projector: we bring our
own light. A line laser draws a bright stripe on the part; BOTH Fox cameras see
it; we rectify with the factory stereo calibration, extract the sub-pixel line
in each image, and because corresponding points share a rectified row the
disparity gives metric depth directly — no laser-plane calibration needed. The
turntable rotates the part through known angles; a one-time turntable-axis
calibration places every slice into one coordinate frame.

Line extraction (per-row intensity-weighted centroid) is ported from the Horus
project (GPLv2, Mundo Reader S.L.).

Pipeline per turntable step:
    laser on -> capture A/B -> rectify -> extract line in both ->
    match by row -> disparity -> reproject (metric slice) -> rotate into
    the common frame by the known angle about the calibrated axis -> laser off.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import open3d as o3d

from .config import CONFIG_DIR
from .stereo import Reconstructor, StereoCalibration, find_board, BOARD_COLS, BOARD_ROWS, SQUARE_MM

AXIS_FILE = CONFIG_DIR / "turntable_axis.json"


# --------------------------------------------------------------------------
# Laser line extraction (sub-pixel column per row)
# --------------------------------------------------------------------------
def extract_laser_line(image: np.ndarray, threshold: int = 40,
                       window: int = 25) -> tuple[np.ndarray, np.ndarray]:
    """Return (rows v, sub-pixel columns u) of the laser line.

    Red channel, per-row **background-subtracted local-peak** detection: each
    row's median is subtracted, so the laser is found as a local bright peak
    regardless of the camera's overall brightness. This makes one threshold work
    across two cameras with very different exposure response, and rejects a
    uniformly bright ("flooded") frame -- a flat row has no peak above its own
    background. `threshold` is now a contrast (peak-above-background) value.
    """
    if image.ndim == 3:
        chan = image[:, :, 2].astype(np.float32)   # red in BGR
    else:
        chan = image.astype(np.float32)
    h, w = chan.shape
    base = np.median(chan, axis=1, keepdims=True)          # per-row background
    sig = np.clip(chan - base, 0, None)                    # ambient removed
    peak_col = sig.argmax(axis=1)
    peak_val = sig[np.arange(h), peak_col]
    rows = np.where(peak_val >= threshold)[0]
    if rows.size == 0:
        return rows, np.array([])
    u = np.empty(rows.size, dtype=np.float64)
    for i, r in enumerate(rows):
        c = peak_col[r]
        c0, c1 = max(0, c - window), min(w, c + window + 1)
        seg = sig[r, c0:c1].copy()
        seg[seg < peak_val[r] * 0.4] = 0.0                 # keep only near the peak
        cols = np.arange(c0, c1, dtype=np.float64)
        s = seg.sum()
        u[i] = (cols * seg).sum() / s if s > 0 else float(c)
    return rows, u


def open_camera_manual(ref, exposure: int, width: int = 1280, height: int = 720):
    """Open a Fox camera with a fixed short exposure. Forces the DirectShow
    backend on Windows -- MSMF ignores the log2 CAP_PROP_EXPOSURE convention, so
    the laser can only be isolated from ambient via DSHOW here."""
    if sys.platform == "win32" and isinstance(ref.index, int):
        cap = cv2.VideoCapture(ref.index, cv2.CAP_DSHOW)
    else:
        cap = cv2.VideoCapture(ref.index, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)   # manual
    cap.set(cv2.CAP_PROP_EXPOSURE, exposure)
    return cap


def _reproject(u: np.ndarray, v: np.ndarray, disp: np.ndarray, Q: np.ndarray) -> np.ndarray:
    """Reproject rectified (u, v, disparity) to 3D via the Q matrix."""
    n = u.shape[0]
    hom = np.stack([u, v, disp, np.ones(n)], axis=1) @ Q.T
    xyz = hom[:, :3] / hom[:, 3:4]
    return xyz


def triangulate_slice(img_a: np.ndarray, img_b: np.ndarray, recon: Reconstructor,
                      threshold: int = 40, z_range=(80.0, 600.0)) -> np.ndarray:
    """One laser-lit stereo pair -> Nx3 metric points (rectified reference frame)."""
    ra = cv2.remap(img_a, recon.map1[0], recon.map1[1], cv2.INTER_LINEAR)
    rb = cv2.remap(img_b, recon.map2[0], recon.map2[1], cv2.INTER_LINEAR)
    va, ua = extract_laser_line(ra, threshold)
    vb, ub = extract_laser_line(rb, threshold)
    if va.size == 0 or vb.size == 0:
        return np.empty((0, 3))
    # corresponding points share a rectified row -> match by row
    ub_by_row = {int(v): u for v, u in zip(vb, ub)}
    U, V, D = [], [], []
    for v, u in zip(va, ua):
        ub_r = ub_by_row.get(int(v))
        if ub_r is None:
            continue
        d = u - ub_r
        if d > 0.5:                      # positive disparity only
            U.append(u); V.append(float(v)); D.append(d)
    if not U:
        return np.empty((0, 3))
    xyz = _reproject(np.array(U), np.array(V), np.array(D), recon.Q)
    z = xyz[:, 2]
    return xyz[(z > z_range[0]) & (z < z_range[1])]


def triangulate_auto(img_a, img_b, recon: Reconstructor, recon_swapped: Reconstructor,
                     threshold: int = 40, z_range=(80.0, 600.0)):
    """Try both camera orderings and keep the one giving more in-range points.
    Returns (points, swapped) so the caller can lock the resolved order."""
    p_norm = triangulate_slice(img_a, img_b, recon, threshold, z_range)
    p_swap = triangulate_slice(img_b, img_a, recon_swapped, threshold, z_range)
    if len(p_swap) > len(p_norm):
        return p_swap, True
    return p_norm, False


def line_quality(rows: np.ndarray, cols: np.ndarray, height: int) -> tuple[float, str]:
    """Score how much a detected (rows, cols) looks like a real laser line (0..1)
    plus a short diagnosis. Continuous smooth stripe -> high; nothing or a
    frame-filling ambient wash -> low."""
    n = len(rows)
    if n == 0:
        return 0.0, "no line (raise exposure / lower threshold)"
    if n > 0.85 * height:
        return 0.1, "ambient flooding (darken room / raise threshold)"
    # coherence: fraction of consecutive detected rows with a small column jump
    order = np.argsort(rows)
    du = np.abs(np.diff(cols[order]))
    coherence = float((du < 15).mean()) if du.size else 0.0
    coverage = min(1.0, n / (0.4 * height))
    score = 0.5 * coherence + 0.5 * coverage
    if coherence < 0.5:
        return score, "scattered / noisy (dim ambient, check focus)"
    return score, "clean line"


# --------------------------------------------------------------------------
# Turntable axis (so rotational slices assemble into one cloud)
# --------------------------------------------------------------------------
@dataclass
class TurntableAxis:
    point: np.ndarray       # a point on the rotation axis (mm, camera-A rect frame)
    direction: np.ndarray   # unit axis direction

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        AXIS_FILE.write_text(json.dumps(
            {"point": self.point.tolist(), "direction": self.direction.tolist()}, indent=2))

    @classmethod
    def load(cls) -> Optional["TurntableAxis"]:
        if not AXIS_FILE.exists():
            return None
        d = json.loads(AXIS_FILE.read_text())
        return cls(np.array(d["point"]), np.array(d["direction"]))


def _rotation_about_axis(axis_dir: np.ndarray, angle_rad: float) -> np.ndarray:
    return cv2.Rodrigues(axis_dir / np.linalg.norm(axis_dir) * angle_rad)[0]


def place_slice(points: np.ndarray, axis: TurntableAxis, angle_deg: float) -> np.ndarray:
    """Rotate a slice captured at +angle back into the angle=0 frame."""
    if points.size == 0:
        return points
    R = _rotation_about_axis(axis.direction, -np.deg2rad(angle_deg))
    return (points - axis.point) @ R.T + axis.point


def calibrate_axis_from_checkerboard(
    recon: Reconstructor,
    poses: list[tuple[float, np.ndarray]],
) -> TurntableAxis:
    """Fit the turntable axis from checkerboard captures at known angles.

    `poses` is a list of (angle_deg, rectified_camera_A_image). The board's
    origin traces a circle as the table turns; axis = that circle's centre/normal.
    """
    objp = np.zeros((BOARD_COLS * BOARD_ROWS, 3), np.float32)
    objp[:, :2] = np.mgrid[0:BOARD_COLS, 0:BOARD_ROWS].T.reshape(-1, 2) * SQUARE_MM
    K = recon.calib.K1  # rectified images ~ use P1 intrinsics; K1 is a fair approx
    origins = []
    for angle, img in poses:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        corners = find_board(gray)
        if corners is None:
            continue
        ok, rvec, tvec = cv2.solvePnP(objp, corners, K, None)
        if ok:
            origins.append(tvec.ravel())
    if len(origins) < 3:
        raise RuntimeError(f"Need >=3 board detections to fit the axis; got {len(origins)}")
    P = np.array(origins)
    centroid = P.mean(axis=0)
    # plane normal (axis direction) = smallest singular vector of centered points
    _, _, vh = np.linalg.svd(P - centroid)
    normal = vh[2]
    # fit circle centre in the plane: project, least-squares circle
    e1 = vh[0]; e2 = vh[1]
    xy = np.stack([(P - centroid) @ e1, (P - centroid) @ e2], axis=1)
    A = np.hstack([2 * xy, np.ones((len(xy), 1))])
    b = (xy ** 2).sum(axis=1)
    cx, cy, _ = np.linalg.lstsq(A, b, rcond=None)[0]
    centre = centroid + cx * e1 + cy * e2
    return TurntableAxis(point=centre, direction=normal / np.linalg.norm(normal))


# --------------------------------------------------------------------------
# Scan loop
# --------------------------------------------------------------------------
def laser_aim(turntable, laser: int = 0, exposure: int = -11, threshold: int = 60):
    """Interactive aiming assistant. Shows both cameras with the detected laser
    line, a per-camera quality score, live depth, and a big READY / not-ready
    banner telling you exactly what to fix. Tune without restarting:

        q       quit
        [ / ]   exposure darker / brighter
        - / =   threshold down / up
        l       switch active laser (0/1)
        w       switch to the other laser and back (occlusion check)

    READY means both cameras see a clean line AND it triangulates to a sane
    depth -- i.e. you can scan. The camera-order ambiguity is auto-resolved.
    """
    from . import fox_calibration
    from .capture.camera import find_fox_cameras

    fox = fox_calibration.load()
    if fox is None:
        raise RuntimeError("No factory calibration. Run 'foxstudio calib' first.")
    recon = Reconstructor(fox.as_stereo())
    recon_sw = Reconstructor(fox.as_stereo().swapped())
    refs = find_fox_cameras()
    if len(refs) < 2:
        raise RuntimeError("Both Fox cameras required. Close JMStudio.")
    cap_a = open_camera_manual(refs[0], exposure)
    cap_b = open_camera_manual(refs[1], exposure)
    turntable.laser(laser, True)
    height = fox.image_size[1]
    W = 520

    def banner(combo, text, ok):
        color = (60, 180, 60) if ok else (40, 40, 200)
        cv2.rectangle(combo, (0, 0), (combo.shape[1], 34), color, -1)
        cv2.putText(combo, text, (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    try:
        while True:
            cap_a.grab(); cap_b.grab()
            oka, fa = cap_a.retrieve(); okb, fb = cap_b.retrieve()
            if not (oka and okb):
                continue
            panels, scores, diags = [], [], []
            for label, img, mp in (("A", fa, recon.map1), ("B", fb, recon.map2)):
                rect = cv2.remap(img, mp[0], mp[1], cv2.INTER_LINEAR)
                v, u = extract_laser_line(rect, threshold)
                score, diag = line_quality(v, u, height)
                scores.append(score); diags.append(diag)
                vis = rect.copy() if rect.ndim == 3 else cv2.cvtColor(rect, cv2.COLOR_GRAY2BGR)
                col = (0, 255, 0) if score > 0.4 else (0, 200, 255)
                for vv, uu in zip(v, u):
                    cv2.circle(vis, (int(round(uu)), int(vv)), 1, col, -1)
                vis = cv2.resize(vis, (W, int(W * rect.shape[0] / rect.shape[1])))
                cv2.putText(vis, f"cam {label}  q={score:.2f}  {diag}", (8, 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
                panels.append(vis)

            pts, swapped = triangulate_auto(fa, fb, recon, recon_sw, threshold)
            med_z = float(np.median(pts[:, 2])) if len(pts) else 0.0
            combo = cv2.hconcat(panels)

            ready = scores[0] > 0.4 and scores[1] > 0.4 and len(pts) >= 40 and 80 < med_z < 600
            if ready:
                msg = f"READY  --  {len(pts)} pts @ {med_z:.0f} mm   (order: {'B-left' if swapped else 'A-left'})"
            elif min(scores) <= 0.4:
                weak = "A" if scores[0] <= scores[1] else "B"
                msg = f"fix cam {weak}: {diags[0] if weak=='A' else diags[1]}"
            elif len(pts) < 40:
                msg = "both see a line but they don't overlap -- aim both cameras at the SAME lit patch"
            else:
                msg = f"depth {med_z:.0f} mm out of range -- move object to ~150-400 mm"
            banner(combo, msg, ready)
            cv2.putText(combo, f"exp {exposure}  thr {threshold}  laser {laser}   "
                               "[ ] exp   - = thr   l laser   q quit",
                        (12, combo.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
            cv2.imshow("FoxStudio laser aim  [ cam A | cam B ]", combo)

            k = cv2.waitKey(30) & 0xFF
            if k == ord("q"):
                break
            elif k == ord("["):
                exposure -= 1
            elif k == ord("]"):
                exposure += 1
            elif k == ord("-"):
                threshold = max(1, threshold - 5)
            elif k == ord("="):
                threshold = min(255, threshold + 5)
            elif k in (ord("l"), ord("w")):
                turntable.laser(laser, False); laser ^= 1; turntable.laser(laser, True)
                continue
            if k in (ord("["), ord("]")):
                cap_a.set(cv2.CAP_PROP_EXPOSURE, exposure)
                cap_b.set(cv2.CAP_PROP_EXPOSURE, exposure)
    finally:
        cap_a.release(); cap_b.release()
        turntable.laser(laser, False)
        cv2.destroyAllWindows()


def laser_scan(
    project,
    turntable,
    steps: int = 200,
    lasers=(0, 1),
    threshold: int = 40,
    settle: float = 0.25,
    exposure: int = -11,
    log=print,
) -> o3d.geometry.PointCloud:
    """Full turntable laser scan. Returns a fused point cloud (also saved to raw/).

    Requires: factory calibration (foxstudio calib) and a turntable-axis
    calibration (foxstudio turntable-calibrate). Alternates the two lasers per
    step to reduce occlusion.
    """
    from . import fox_calibration, meshio
    from .capture.camera import find_fox_cameras, open_camera

    fox = fox_calibration.load()
    if fox is None:
        raise RuntimeError("No factory calibration. Run 'foxstudio calib' first.")
    axis = TurntableAxis.load()
    if axis is None:
        raise RuntimeError("No turntable axis. Run 'foxstudio turntable-calibrate' first.")
    recon = Reconstructor(fox.as_stereo())
    recon_sw = Reconstructor(fox.as_stereo().swapped())

    refs = find_fox_cameras()
    if len(refs) < 2:
        raise RuntimeError("Both Fox cameras required. Close JMStudio.")
    cap_a = open_camera_manual(refs[0], exposure)
    cap_b = open_camera_manual(refs[1], exposure)

    merged = o3d.geometry.PointCloud()
    step_deg = 360.0 / steps
    try:
        turntable.reset_origin()
        for i in range(steps):
            angle = i * step_deg
            for li in lasers:
                turntable.laser(li, True)
                import time; time.sleep(settle)
                for _ in range(2):
                    cap_a.grab(); cap_b.grab()
                ok_a, fa = cap_a.retrieve(); ok_b, fb = cap_b.retrieve()
                turntable.laser(li, False)
                if not (ok_a and ok_b):
                    continue
                slice_pts, _ = triangulate_auto(fa, fb, recon, recon_sw, threshold)
                placed = place_slice(slice_pts, axis, angle)
                if placed.size:
                    pc = o3d.geometry.PointCloud()
                    pc.points = o3d.utility.Vector3dVector(placed)
                    merged += pc
            if i % max(1, steps // 20) == 0:
                log(f"  {i}/{steps} ({angle:.0f} deg), {len(merged.points):,} pts")
            turntable.rotate(step_deg)
    finally:
        cap_a.release(); cap_b.release()
        turntable.lasers_off()
        turntable.disable_motor()

    if len(merged.points) > 1000:
        merged, _ = merged.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    out = project.path / "raw" / "laser_scan.ply"
    meshio.save(merged, out)
    log(f"Laser scan complete: {len(merged.points):,} points -> {out.name}")
    return merged
