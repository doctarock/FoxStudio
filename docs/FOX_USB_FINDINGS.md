# 3DMakerpro Fox — USB investigation findings

Probed 2026-07-07 on Windows 11 with the Fox connected and JMStudio 2.6.14.0210 installed.

> **TL;DR (read this first — the doc below is a chronological investigation and
> some early hypotheses are later overturned):** the vendor UVC Extension Unit
> turned out to be a **red herring** — USBPcap capture shows JMStudio drives the
> whole scan with **100% standard UVC** (it commits format 1 / frame 2 = 640×480
> YUY2 @ 10 fps, then sets exposure and gain) and never writes the extension unit
> or the HID device. Depth is computed host-side from a projected pattern. The one
> thing still **unsolved**: what enables the projector. It is *not* the extension
> unit, HID, resolution, frame rate, dual-streaming, or exposure — all ruled out.
> **If you've cracked the projector-enable on a similar Sonix structured-light
> scanner, that's the feedback I'm after.**

## Answer: yes, the Fox exposes standard USB camera streams

The Fox enumerates as **three Sonix Technology (VID `0x0C45`) devices**:

| Device | Interface | What it is |
|---|---|---|
| `USB\VID_0C45&PID_636A` | UVC camera `<SERIAL>_A` | Standard USB Video Class camera |
| `USB\VID_0C45&PID_636B` | UVC camera `<SERIAL>_B` | Standard USB Video Class camera |
| `USB\VID_0C45&PID_672E` | HID (keyboard + consumer + vendor collections) | Carries no device serial and looks like a keyboard-class device; Sonix VID is common in keyboards. **Later confirmed unrelated to scanning** — JMStudio never touches it (see USBPcap section). |

Verified with OpenCV: both cameras open with the stock OS driver (MSMF on
Windows; they will appear as V4L2 `/dev/video*` on Linux) and stream
**YUY2 at up to 1280×720**. Default mode is 640×480. No vendor driver is
needed — JMStudio's bundled "drivers" are generic camera driver installers
plus a WCH CH343 USB-serial driver (used for the turntable accessory).

## What is NOT on the wire

There is **no depth stream**. The Fox is a single-shot structured-light
scanner: depth is computed **on the host** from what the two cameras see of a
projected pattern. Projector/exposure control is a vendor protocol carried
over a **UVC Extension Unit** on each camera's VideoControl interface
(confirmed below). JMStudio's `LightP` setting in `scaner_settings_*.ini` is
the projector-power knob that travels over this channel.

## Confirmed: the projector control path (UVC Extension Unit)

Reading the cameras' USB descriptors (libusb, no device claim needed) shows a
real vendor Extension Unit on VideoControl interface 0 of **both** cameras:

    Extension Unit  unitID = 3   bNumControls = 8
    GUID = {28F03370-6311-4A2E-BA2C-6890EB334016}

That exact GUID is embedded in **`SonixCamera.dll`** (Sonix is the camera-ASIC
vendor; VID 0x0C45), which is JMStudio's low-level control layer. Its exported
API is the whole vendor surface:

    SonixCam_XuRead / SonixCam_XuWrite         raw extension-unit access
    SonixCam_ControlSet / ControlGet / ...Range  named controls
    SonixCam_AsicRegisterRead / Write          Sonix ASIC registers
    SonixCam_SensorRegisterRead / Write        image-sensor registers
    SonixCam_SerialFlashRead / Write           on-board flash
    SonixCam_GetParamTableAddr / SetParamTable... parameter table in flash

The DLL string `I2C64XUData` shows the XU tunnels a 64-byte I2C payload — i.e.
an XU write addresses the projector/LED driver over I2C. Three candidate XU
GUIDs sit in a table in the DLL (different firmware variants); the one above is
the one this unit actually enumerates.

So driving the projector natively = send an XU SET (control selector in 1..8,
64-byte I2C payload) to unit 3, either through `SonixCam_XuWrite` or directly
via the Windows UVC KsProperty interface (property set = the XU GUID). The
remaining unknown is which of the 8 selectors is projector power and the exact
I2C payload — recoverable by capturing JMStudio's XU traffic (USBPcap) or
disassembling `SonixCam_ControlSet` around the `LightP` path.

### Read-only control map (`foxstudio xu`)

Querying the XU through the Windows `IKsControl`/KsProperty interface (no
writes, stream never started; reproducible with `foxstudio xu`) gives the live
control map. The XU is topology node 1 (`KSNODETYPE_DEV_SPECIFIC`). Typical
values on this unit, camera A:

    selector 0  GET      len 22  = XU GUID + control-count/version (identity)
    selector 1  GET/SET  len 4   volatile  (low byte increments run-to-run)
    selector 2  GET/SET  len 8   semi-volatile
    selector 3  GET/SET  len 11  first byte increments run-to-run (live)
    selector 4  GET/SET  len 11  stable; reads 00 00 00 .. when idle
    selector 5  GET/SET  len 11  stable
    selectors 6-9            ERROR_NOT_FOUND (0x80070490)

Both cameras expose the same XU with independent state (each has its own
controller). Several controls carry a trailing `40 42 0F 00` = 1,000,000
(a µs/clock-looking constant). Selectors 1 and 3 are free-running counters
(pure noise); 0 is identity; 2 tracks exposure/gain.

### Observed under JMStudio (`foxstudio xu --watch`, read-only)

Watching both cameras' XU while JMStudio ran scans (no writes from us; property
reads work fine while JMStudio streams) isolates the projector/pattern control:

**Selectors 4 and 5, on BOTH cameras, are the only config controls that ever
move**, and they moved exactly once — a brief (~0.4 s) synchronized pulse at the
start of a capture, then reverted:

    camera A  sel4: 00 00 00 |ff ff| 42 0f 00 00 00 00  ->  00 00 00 |02 40| 42 0f 00 00 00 00
    camera A  sel5: 2d 04 01 |ff ff| 42 0f 00 00 00 00  ->  2d 04 01 |02 40| 42 0f 00 00 00 00
    camera B  sel4: 00 00 00 |88 40| 42 0f 00 00 00 00  ->  00 00 00 |00 40| 42 0f 00 00 00 00
    camera B  sel5: 2d 04 01 |88 40| 42 0f 00 00 00 00  ->  2d 04 01 |00 40| 42 0f 00 00 00 00

The change is confined to bytes [3:5] (each camera has its own idle value there).
Because it is a brief pulse rather than a level held for the whole scan, selector
4/5 read as a per-capture **arm/trigger**, not a sustained "projector power" knob;
the continuous illumination is likely driven by the streaming/pattern mode once
armed. Selectors 4 and 5 on camera A are therefore the concrete control surface
to replay.

### Bounded write test (done, safe, negative)

With go-ahead, a careful bounded write test was run: while streaming camera A,
read+save selectors 4/5, replay JMStudio's captured active values verbatim
(`SET_CUR` to unit 3), capture a 30-frame burst, then restore. It was fully
clean and reversible (values restored; device settled back to idle `ff ff`).

**Result: the projector did not fire.** All frames stayed at the projector-off
baseline (Laplacian variance ~152, unchanged). And the read-back exposed byte 3
of selector 4 stepping `ff -> 01 -> 02` across sessions — i.e. it behaves like a
**counter/status field, not a command input**. Conclusion: selectors 4/5 are most
likely status/config *readback* that changes as a side-effect of scanning, not
the projector control. `--watch` can only see values change; it cannot tell a
device write from a device-side status update, and this test indicates the latter.

Also ruled out (read-only): streaming at the scan resolution (1280×720) alone does
not enable the projector — native captures at that mode show no pattern.

**Net:** the projector control is not a simple XU value replay.

### USBPcap capture of JMStudio (done) — the extension unit is a red herring

A full USBPcap capture of JMStudio running a scan (decoded with
`scratchpad/parse_usbpcap.py`) settles the direction question the read-only side
could not. During an entire scan, JMStudio's traffic to the Fox was **100 %
standard UVC, and it never touched the extension unit (unit 3) or the HID device
(`0c45:672e`) at all:**

    VS_PROBE/COMMIT (iface 1, sel 1/2): format index 1, frame index 2,
                                        dwFrameInterval 1,000,000 (10 fps)
    Camera Terminal exposure  (unit 1, sel 4): ~2-4 ms (short)
    Processing Unit gain      (unit 2, sel 4)
    ...then isochronous video. No SET to unit 3, no HID reports.

So the whole extension-unit line of investigation — and the earlier bounded write
test — was aimed at the wrong entity; that is why the write did nothing. The
`0c45:672e` HID device is confirmed **unrelated to scanning** (silent throughout).

### Second capture (from cold) + replication attempts — projector NOT cracked

A second USBPcap capture taken **from a cold power-cycle** (Fox just replugged,
JMStudio never opened, then JMStudio launched during the window) showed the same
thing at device connect: **100 % standard UVC, no extension-unit write, no HID
output report** (the HID device only *sends* input reports, it is never
commanded). JMStudio's committed video format is **format 1 / frame 2 = 640×480
YUY2 @ 10 fps** (`dwMaxVideoFrameSize` 0x00096000 = 614400 = 640×480×2), then
SET_INTERFACE to start streaming, then CT AE-mode manual + exposure (~2–4 ms) +
PU gain. Nothing else.

Replication attempts via our own standard-UVC streaming, checked with a rigorous
discriminator (very short exposure isolates a bright projected pattern as
dots-on-black; ambient goes black):

| variable tried | result |
|---|---|
| 640×480, 1280×720, other resolutions | projector off |
| 10 fps (JMStudio's rate) vs 30 fps | off in both |
| single camera vs **both cameras streaming** (controlled, same exposure) | identical — both pure black |
| exposure ladder −7…−13 | collapses to black; no dots-on-black |

Earlier apparent "pattern" sightings were **confounds** — a shiny metal mesh
glinting at short exposure, textured surfaces, or exposure settings not applying
in some paths — not confirmed projector activity. The one time the projector was
genuinely latched on (bold stripes on a matte item) followed a JMStudio scan and
did **not** reproduce cold.

**Honest status:** the projector-enable was not reproduced. It is definitively
**not** the extension unit, the HID device, the resolution, the frame rate, the
dual-stream condition, or exposure. The only remaining untested variable is the
**exact UVC media type / probe-commit bytes and control ordering** committed via
a DirectShow graph (OpenCV negotiates its own and may not match format 1/frame 2
precisely) — a significant instrumentation effort with uncertain payoff. Pending
that, **JMStudio remains the structured-light capture path**; FoxStudio's native
passive-stereo + factory calibration + full downstream pipeline stands on its own.

## Confirmed: the Fox is single-shot structured light, and the calibration is on disk

JMStudio caches the **full factory calibration per unit** at
`<install>/download/calib_<SERIAL>.txt` in plain text. For this unit it contains:

- image size 1280×720;
- both cameras as pinhole+distortion models (fx ≈ 2799 px), and the
  **projector modeled as a third camera**;
- extrinsics: **camera↔camera baseline 32.36 mm**, camera↔projector 57.6 mm;
- a 128-entry projector column→ray lookup table;
- the **fixed projected pattern**: 246 rows × 6 symbols over a 4-symbol
  alphabet — a De Bruijn-style stripe code that is the same every frame
  (single-shot), so it can be decoded without capturing a sequence.

FoxStudio parses this file directly (`foxstudio/fox_calibration.py`) and uses
it for native scanning, so **no checkerboard calibration is required** — the
factory numbers are better than a user calibration. The checkerboard flow
remains as a fallback for units whose file is missing.

Consequence for the roadmap: native scanning does not need passive-stereo
texture. Once the projector is on, the two cameras see a strong known pattern,
so either (a) plain SGBM stereo works well, or (b) the pattern can be decoded
against the stored code + projector calibration for full structured-light
depth. The only missing piece is the one-line XU command that turns the
projector on — a device **write**, held pending explicit go-ahead.

## What direct capture can do today

`foxstudio capture` grabs synchronized A/B stereo frame pairs straight from
the UVC streams (close JMStudio first — it holds the cameras exclusively).

Path to full independent depth reconstruction, in increasing effort:

1. **Passive stereo** — calibrate the A/B pair once (OpenCV
   `stereoCalibrate` with a printed checkerboard), then compute disparity
   with `StereoSGBM` and back-project to a point cloud. Works without
   touching the HID channel, but quality on textureless parts is poor.
2. **Pattern-assisted stereo** — with the pattern projector on (leave a scan
   preview running, or replay the control commands once learned), textured
   stereo works much better with the same calibration.
3. **Full protocol** — capture the USB control traffic JMStudio sends
   (Wireshark + USBPcap filtered to the two camera devices) to learn the
   UVC extension-unit requests for projector power/pattern sequencing, then
   drive the whole scan loop independently. This is the reverse engineering
   long game.

The practical near-term workflow remains: scan in JMStudio, export
STL/OBJ/PLY/ASC, and run everything downstream offline with FoxStudio.

## Other notes from the JMStudio install

- `C:\Program Files (x86)\JMStudio\<ver>\Config\scaner_settings_*.ini` holds
  per-model tuning (camera brightness, projector power `LightP`, Poisson
  reconstruction defaults). Useful reference values for our own Poisson step:
  `resolution=1.7`, `trim_value=2.0`, `islandAreaRatio=0.00005`.
- Bundled SDKs for MindVision/HK industrial cameras are for other scanner
  models in the JMStudio family, not the Fox.
- Turntable control is a CH343 USB-serial device (COM port) when connected.
