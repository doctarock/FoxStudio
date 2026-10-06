"""Report what the Fox exposes over USB on this machine (`foxstudio probe`)."""
from __future__ import annotations

import subprocess
import sys

from .camera import FOX_VID, FOX_CAM_PIDS, FOX_HID_PID, find_fox_cameras


def _windows_fox_devices() -> list[str]:
    ps = (
        "Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match 'VID_0C45' } "
        "| ForEach-Object { $_.Status + '  ' + $_.Class + '  ' + $_.FriendlyName + '  ' + $_.InstanceId }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=30)
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _linux_fox_devices() -> list[str]:
    try:
        out = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10)
        return [ln for ln in out.stdout.splitlines() if "0c45" in ln.lower()]
    except (OSError, subprocess.TimeoutExpired):
        return []


def report() -> str:
    lines = [
        "Fox USB probe",
        "=============",
        f"Known interfaces (VID {FOX_VID:04X}):",
        f"  PID {FOX_CAM_PIDS[0]:04X} -> UVC camera A (standard video class, serial JMM...)",
        f"  PID {FOX_CAM_PIDS[1]:04X} -> UVC camera B (standard video class, serial JMM...)",
        f"  PID {FOX_HID_PID:04X} -> HID device (uncertain: keyboard/vendor collections, no JMM"
        " serial; unplug the Fox to confirm whether it belongs to the scanner)",
        "",
        "Devices present now:",
    ]
    devices = _windows_fox_devices() if sys.platform == "win32" else _linux_fox_devices()
    lines += [f"  {d}" for d in devices] or ["  (no Sonix VID 0C45 devices found -- is the Fox plugged in?)"]

    lines += ["", "Cameras resolved for capture:"]
    refs = find_fox_cameras()
    if refs:
        lines += [f"  {r.label}: {r.name} (open target: {r.index})" for r in refs]
    else:
        lines.append("  none found. If the Fox is connected, close JMStudio and re-run.")
    lines += [
        "",
        "Notes:",
        "  - There is NO depth stream on the wire. Depth is computed on the host by",
        "    JMStudio from the projected pattern seen by the two cameras.",
        "  - Direct capture grabs synchronized A/B frame pairs (foxstudio capture).",
        "    Full depth reconstruction additionally needs projector/pattern control",
        "    (most likely UVC extension units on the camera interfaces) plus the",
        "    factory stereo calibration.",
    ]
    return "\n".join(lines)
