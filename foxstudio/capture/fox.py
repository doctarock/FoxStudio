"""Direct stereo-pair capture from the Fox into a project's captures/ folder.

Grabs near-simultaneous frames from cameras A and B. Pairs are saved as
    captures/<session>/pair_NNNN_A.png / pair_NNNN_B.png
with a session.json noting timestamps and settings. These pairs are the raw
material for stereo reconstruction; with a calibrated pair (see
docs/FOX_USB_FINDINGS.md) they can be turned into disparity/point clouds via
OpenCV StereoSGBM + Open3D.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import cv2

from .camera import find_fox_cameras, open_camera


def capture_pairs(
    out_dir: Path,
    pairs: int = 1,
    interval: float = 0.5,
    width: int | None = None,
    height: int | None = None,
    preview: bool = False,
) -> dict:
    refs = find_fox_cameras()
    if len(refs) < 2:
        raise IOError(
            f"Found {len(refs)} of 2 Fox cameras. Plug in the Fox and close JMStudio "
            "(it holds the streams exclusively). Run 'foxstudio probe' for details.")

    session = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = Path(out_dir) / session
    session_dir.mkdir(parents=True, exist_ok=True)

    cap_a = open_camera(refs[0], width, height)
    cap_b = open_camera(refs[1], width, height)
    records = []
    try:
        # warm up auto-exposure
        for _ in range(10):
            cap_a.read()
            cap_b.read()
        for n in range(pairs):
            # drain buffered frames so both reads are current
            for _ in range(2):
                cap_a.grab()
                cap_b.grab()
            t0 = time.time()
            ok_a, frame_a = cap_a.read()
            ok_b, frame_b = cap_b.read()
            if not (ok_a and ok_b):
                records.append({"pair": n, "error": "frame grab failed"})
                continue
            fa = session_dir / f"pair_{n:04d}_A.png"
            fb = session_dir / f"pair_{n:04d}_B.png"
            cv2.imwrite(str(fa), frame_a)
            cv2.imwrite(str(fb), frame_b)
            records.append({
                "pair": n,
                "timestamp": round(t0, 3),
                "a": fa.name, "b": fb.name,
                "shape": list(frame_a.shape),
            })
            if preview:
                both = cv2.hconcat([frame_a, frame_b])
                cv2.imshow("Fox A | B  (any key = next, q = stop)", both)
                key = cv2.waitKey(0 if interval <= 0 else int(interval * 1000))
                if key in (ord("q"), 27):
                    break
            elif interval > 0 and n < pairs - 1:
                time.sleep(interval)
    finally:
        cap_a.release()
        cap_b.release()
        if preview:
            cv2.destroyAllWindows()

    manifest = {
        "session": session,
        "cameras": [{"label": r.label, "name": r.name} for r in refs],
        "pairs": records,
    }
    (session_dir / "session.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["dir"] = str(session_dir)
    return manifest
