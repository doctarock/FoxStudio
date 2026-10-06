"""Locate and open the Fox's two UVC cameras on Windows or Linux.

Verified on this Fox (serial JMM8004902):
  - USB\\VID_0C45&PID_636A  ->  UVC camera "JMM8004902_A"  (YUY2, up to 1280x720)
  - USB\\VID_0C45&PID_636B  ->  UVC camera "JMM8004902_B"  (YUY2, up to 1280x720)
  - USB\\VID_0C45&PID_672E  ->  HID composite (possibly unrelated; see docs/FOX_USB_FINDINGS.md)

VID 0x0C45 is Sonix Technology. Both cameras are plain USB Video Class devices,
so OpenCV can open them without JMStudio (close JMStudio first -- it holds the
streams exclusively while connected).
"""
from __future__ import annotations

import glob
import sys
from dataclasses import dataclass
from typing import Optional

import cv2

from ..config import load_config

FOX_VID = 0x0C45
FOX_CAM_PIDS = (0x636A, 0x636B)
FOX_HID_PID = 0x672E


@dataclass
class CameraRef:
    label: str          # "A" or "B"
    name: str           # device name reported by the OS
    index: object       # int index (Windows) or /dev/video* path (Linux)


def _windows_device_names() -> list[str]:
    from pygrabber.dshow_graph import FilterGraph
    return FilterGraph().get_input_devices()


def _find_windows(cfg: dict) -> list[CameraRef]:
    names = _windows_device_names()
    refs = []
    for label, key in (("A", "camera_a"), ("B", "camera_b")):
        pattern = cfg[key]
        for i, name in enumerate(names):
            if pattern.lower() in name.lower():
                refs.append(CameraRef(label, name, i))
                break
    return refs


def _find_linux(cfg: dict) -> list[CameraRef]:
    refs = []
    # /dev/v4l/by-id/ names embed the device name, e.g. ...JMM8004902_A...-video-index0
    for label, key in (("A", "camera_a"), ("B", "camera_b")):
        pattern = cfg[key].lower()
        for link in sorted(glob.glob("/dev/v4l/by-id/*")):
            if pattern in link.lower() and link.endswith("index0"):
                refs.append(CameraRef(label, link.rsplit("/", 1)[-1], link))
                break
    return refs


def find_fox_cameras() -> list[CameraRef]:
    cfg = load_config()["capture"]
    if sys.platform == "win32":
        return _find_windows(cfg)
    return _find_linux(cfg)


def open_camera(ref: CameraRef, width: Optional[int] = None,
                height: Optional[int] = None) -> cv2.VideoCapture:
    cfg = load_config()["capture"]
    width = width or cfg["width"]
    height = height or cfg["height"]
    if sys.platform == "win32":
        backends = (cv2.CAP_MSMF, cv2.CAP_DSHOW)
    else:
        backends = (cv2.CAP_V4L2,)
    for backend in backends:
        cap = cv2.VideoCapture(ref.index, backend)
        if not cap.isOpened():
            cap.release()
            continue
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        # Some backends report opened but never deliver (e.g. MSMF on devices
        # another API claimed); prove a frame before accepting.
        ok, _ = cap.read()
        if ok:
            return cap
        cap.release()
    raise IOError(f"Cannot open Fox camera {ref.label} ({ref.name}). "
                  "Close JMStudio if it is running -- it locks the streams.")
