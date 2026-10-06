"""Serial driver for a Ciclop / Horus "ZUM SCAN" scanner board (GRBL firmware).

Drives the turntable stepper and the two line lasers over USB-serial. This is
the same board the open-source Ciclop/Horus scanner uses; FoxStudio reuses it to
add a motorised turntable and a laser-line scanning mode to the Fox.

G-code protocol (115200 baud, each line \\r\\n-terminated, board replies "ok"):
    Ctrl-X (0x18) : soft reset (emits the firmware banner)
    G1 F<n>       : feed rate  (turntable speed, deg/s)
    $120=<n>      : acceleration (deg/s^2)
    M17 / M18     : enable / disable stepper
    G50           : set current position as origin (0 deg)
    G1 X<deg>     : move to absolute angle (degrees)
    M71 T<n>      : laser n on   (n = 1, 2 for the two line lasers)
    M70 T<n>      : laser n off
"""
from __future__ import annotations

import time
from typing import Optional

import serial
import serial.tools.list_ports

BAUD = 115200
# USB-serial chips commonly used on these boards (CH340, FTDI, CP210x, Arduino)
_KNOWN_HWIDS = ("1A86:7523", "1A86:5523", "0403:6001", "10C4:EA60", "2341:")


class TurntableError(RuntimeError):
    pass


def find_port() -> Optional[str]:
    """Best-guess serial port for the scanner board (prefers known USB-serial chips)."""
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        hwid = (p.hwid or "").upper()
        if any(k in hwid for k in _KNOWN_HWIDS):
            return p.device
    return ports[0].device if ports else None


class Turntable:
    def __init__(self, port: Optional[str] = None, speed: float = 200.0,
                 acceleration: float = 200.0, invert: bool = False):
        self.port = port or find_port()
        self.speed = speed
        self.acceleration = acceleration
        self.direction = -1 if invert else 1
        self._sp: Optional[serial.Serial] = None
        self._position = 0.0
        self._motor_on = False
        self._lasers = [False, False]

    # -- connection --------------------------------------------------------
    def connect(self) -> str:
        if not self.port:
            raise TurntableError("No serial port found. Plug in the scanner board.")
        try:
            self._sp = serial.Serial(self.port, BAUD, timeout=2)
        except serial.SerialException as exc:
            raise TurntableError(f"Cannot open {self.port}: {exc}")
        time.sleep(0.2)
        self._sp.reset_input_buffer()
        self._sp.reset_output_buffer()
        self._sp.write(b"\x18\r\n")            # soft reset
        time.sleep(0.4)
        banner = self._sp.read_all().decode("ascii", "ignore").strip()
        # lenient: accept Horus/GRBL banner, but don't hard-fail on a variant
        self._sp.timeout = 0.3
        self._cmd(f"G1F{self.speed:g}")
        self._cmd(f"$120={self.acceleration:g}")
        self.reset_origin()
        return banner

    def disconnect(self):
        if self._sp is not None:
            try:
                self.lasers_off()
                self.disable_motor()
            finally:
                self._sp.close()
                self._sp = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    # -- low level ---------------------------------------------------------
    def _cmd(self, gcode: str, settle: float = 0.0) -> str:
        if self._sp is None:
            raise TurntableError("Not connected")
        self._sp.reset_input_buffer()
        self._sp.write((gcode + "\r\n").encode("ascii"))
        # read until we get a reply line (or timeout)
        reply = ""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            line = self._sp.readline().decode("ascii", "ignore").strip()
            if line:
                reply = line
                break
        if settle:
            time.sleep(settle)
        return reply

    # -- motor -------------------------------------------------------------
    def set_speed(self, deg_per_s: float):
        self.speed = deg_per_s
        self._cmd(f"G1F{deg_per_s:g}")

    def enable_motor(self):
        if not self._motor_on:
            self._cmd("M17", settle=1.0)      # firmware pauses ~1s on enable
            self._cmd(f"G1F{self.speed:g}")
            self._motor_on = True

    def disable_motor(self):
        if self._motor_on:
            self._cmd("M18")
            self._motor_on = False

    def reset_origin(self):
        self._cmd("G50")
        self._position = 0.0

    def move_to(self, degrees: float, wait: bool = True):
        """Rotate to an absolute angle."""
        self.enable_motor()
        self._position = degrees
        self._cmd(f"G1X{degrees * self.direction:f}")
        if wait:
            # rough settle based on travel time + margin
            time.sleep(min(3.0, abs(degrees) / max(self.speed, 1e-3) + 0.3))

    def rotate(self, delta_degrees: float, wait: bool = True):
        """Rotate by a relative angle from the current position."""
        self.move_to(self._position + delta_degrees, wait=wait)

    # -- continuous spin (for use alongside JMStudio) ----------------------
    def spin(self, speed: float, direction: int = 1):
        """Start smooth continuous rotation at `speed` and return immediately.
        Call stop_spin() to halt. Meant to turn a part while JMStudio scans."""
        if self._sp is None:
            raise TurntableError("Not connected")
        self.direction = 1 if direction >= 0 else -1
        self.set_speed(speed)
        self.enable_motor()
        # one very long move at the set feed rate = smooth continuous rotation;
        # sent without waiting for completion (it finishes far in the future)
        far = self._position + self.direction * 1_000_000.0
        self._position = far
        self._sp.reset_input_buffer()
        self._sp.write((f"G1X{far:f}\r\n").encode("ascii"))
        self._spinning = True

    def stop_spin(self):
        """Halt a continuous spin."""
        if self._sp is None:
            return
        self._sp.write(b"!")            # feed hold: decelerate and stop
        time.sleep(0.3)
        self._sp.write(b"\x18\r\n")     # soft reset: abort the pending long move
        time.sleep(0.4)
        self._sp.reset_input_buffer()
        self._position = 0.0
        self._motor_on = False
        self._spinning = False

    @property
    def position(self) -> float:
        return self._position

    # -- lasers ------------------------------------------------------------
    def laser(self, index: int, on: bool):
        """index 0 or 1 -> board lasers T1 / T2."""
        if not 0 <= index < 2:
            raise ValueError("laser index must be 0 or 1")
        self._cmd(f"M71T{index + 1}" if on else f"M70T{index + 1}")
        self._lasers[index] = on

    def lasers(self, on: bool):
        for i in (0, 1):
            self.laser(i, on)

    def lasers_off(self):
        self.lasers(False)
