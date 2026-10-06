"""READ-ONLY probe of the Fox camera's UVC Extension Unit (Windows only).

Queries each vendor extension-unit control's access flags, byte length, and
current value via the Windows KsProperty interface (IKsControl) on the
DirectShow capture filter. Nothing is written to the device and the video
stream is never started -- this only reads the control map, which is the input
needed before any projector-control work.

Findings on unit JMM8004902 (see docs/FOX_USB_FINDINGS.md):
  XU GUID {28F03370-6311-4A2E-BA2C-6890EB334016}, dev-specific topology node.
  selector 0  GET       identity (returns GUID + control count)
  selector 1  GET/SET   4 bytes, volatile (tracks live camera state)
  selector 2  GET/SET   8 bytes, volatile
  selector 3  GET/SET   11 bytes, stable config
  selector 4  GET/SET   11 bytes, stable config (reads 00 00 00 ... when idle)
  selector 5  GET/SET   11 bytes, stable config
  selectors 6+          ERROR_NOT_FOUND
"""
from __future__ import annotations

import sys
from ctypes import (HRESULT, POINTER, Structure, addressof, c_void_p,
                    cast, create_string_buffer, sizeof)
from ctypes.wintypes import DWORD, ULONG

XU_GUID_STR = "{28F03370-6311-4A2E-BA2C-6890EB334016}"

KSPROPERTY_TYPE_GET = 0x00000001
KSPROPERTY_TYPE_SET = 0x00000002
KSPROPERTY_TYPE_BASICSUPPORT = 0x00000200
KSPROPERTY_TYPE_TOPOLOGY = 0x10000000
KSNODETYPE_DEV_SPECIFIC_STR = "{941C7AC0-C559-11D0-8A2B-00A0C9255AC1}"


def _build_interfaces():
    import comtypes
    from comtypes import COMMETHOD, GUID, IUnknown

    class KSP_NODE(Structure):
        _fields_ = [("Set", GUID), ("Id", ULONG), ("Flags", ULONG),
                    ("NodeId", ULONG), ("Reserved", ULONG)]

    class IKsControl(IUnknown):
        _iid_ = GUID("{28F54685-06FD-11D2-B27A-00A0C9223196}")
        _methods_ = [
            COMMETHOD([], HRESULT, "KsProperty",
                      (["in"], c_void_p, "Property"), (["in"], ULONG, "PropertyLength"),
                      (["in", "out"], c_void_p, "PropertyData"), (["in"], ULONG, "DataLength"),
                      (["out"], POINTER(ULONG), "BytesReturned")),
            COMMETHOD([], HRESULT, "KsMethod",
                      (["in"], c_void_p, "m"), (["in"], ULONG, "ml"),
                      (["in", "out"], c_void_p, "md"), (["in"], ULONG, "dl"),
                      (["out"], POINTER(ULONG), "br")),
            COMMETHOD([], HRESULT, "KsEvent",
                      (["in"], c_void_p, "e"), (["in"], ULONG, "el"),
                      (["in", "out"], c_void_p, "ed"), (["in"], ULONG, "dl"),
                      (["out"], POINTER(ULONG), "br")),
        ]

    class IKsTopologyInfo(IUnknown):
        _iid_ = GUID("{720D4AC0-7533-11D0-A5D6-28DB04C10000}")
        _methods_ = [
            COMMETHOD([], HRESULT, "get_NumCategories", (["out"], POINTER(DWORD), "n")),
            COMMETHOD([], HRESULT, "get_Category", (["in"], DWORD, "i"), (["out"], POINTER(GUID), "c")),
            COMMETHOD([], HRESULT, "get_NumConnections", (["out"], POINTER(DWORD), "n")),
            COMMETHOD([], HRESULT, "get_ConnectionInfo", (["in"], DWORD, "i"), (["in"], c_void_p, "p")),
            COMMETHOD([], HRESULT, "get_NodeName", (["in"], DWORD, "i"), (["in"], c_void_p, "b"),
                      (["in"], DWORD, "s"), (["out"], POINTER(DWORD), "l")),
            COMMETHOD([], HRESULT, "get_NumNodes", (["out"], POINTER(DWORD), "n")),
            COMMETHOD([], HRESULT, "get_NodeType", (["in"], DWORD, "i"), (["out"], POINTER(GUID), "t")),
            COMMETHOD([], HRESULT, "CreateNodeInstance", (["in"], DWORD, "i"),
                      (["in"], POINTER(GUID), "iid"), (["out"], POINTER(POINTER(IUnknown)), "o")),
        ]

    return comtypes, GUID, KSP_NODE, IKsControl, IKsTopologyInfo


def probe(name_sub: str = "JMM") -> dict:
    """Return {'filter':..., 'node':..., 'controls':[{selector, access, length, value_hex}]}.

    Read-only. Raises RuntimeError if not on Windows or no matching camera.
    """
    if sys.platform != "win32":
        raise RuntimeError("XU probe is Windows-only (uses DirectShow KsProperty).")
    comtypes, GUID, KSP_NODE, IKsControl, IKsTopologyInfo = _build_interfaces()
    from comtypes import CoInitialize
    from pygrabber.dshow_graph import SystemDeviceEnum, DeviceCategories

    CoInitialize()
    sde = SystemDeviceEnum()
    names = sde.get_available_filters(DeviceCategories.VideoInputDevice)
    idx = next((i for i, n in enumerate(names) if name_sub.lower() in n.lower()), None)
    if idx is None:
        raise RuntimeError(f"No camera matching {name_sub!r} in {names}")
    flt, fname = sde.get_filter_by_index(DeviceCategories.VideoInputDevice, idx)

    topo = flt.QueryInterface(IKsTopologyInfo)
    node_id = None
    for i in range(topo.get_NumNodes()):
        if str(topo.get_NodeType(i)).upper() == KSNODETYPE_DEV_SPECIFIC_STR.upper():
            node_id = i
            break
    if node_id is None:
        raise RuntimeError("No dev-specific (extension unit) topology node found")

    ksctrl = flt.QueryInterface(IKsControl)
    xu = GUID(XU_GUID_STR)

    def query(selector, flags, out_len):
        prop = KSP_NODE()
        prop.Set = xu
        prop.Id = selector
        prop.Flags = flags | KSPROPERTY_TYPE_TOPOLOGY
        prop.NodeId = node_id
        buf = create_string_buffer(out_len)
        try:
            ret = ksctrl.KsProperty(cast(addressof(prop), c_void_p), sizeof(prop),
                                    cast(buf, c_void_p), out_len)
            if isinstance(ret, (list, tuple)):
                ret = ret[-1]
            return True, int(ret), buf.raw
        except comtypes.COMError as exc:
            return False, exc.hresult & 0xFFFFFFFF, b""

    controls = []
    for sel in range(0, 10):
        ok, _, sbuf = query(sel, KSPROPERTY_TYPE_BASICSUPPORT, 4)
        if not ok:
            continue
        access = int.from_bytes(sbuf[:4], "little")
        cap = ("GET" if access & KSPROPERTY_TYPE_GET else "") + \
              ("/SET" if access & KSPROPERTY_TYPE_SET else "")
        gok, glen, gdata = query(sel, KSPROPERTY_TYPE_GET, 64)
        controls.append({
            "selector": sel,
            "access": cap.strip("/"),
            "length": glen if gok else None,
            "value_hex": gdata[:glen].hex() if gok else None,
        })
    return {"filter": fname, "node": node_id, "guid": XU_GUID_STR, "controls": controls}


def watch(name_sub: str = "JMM", seconds: float = 30.0, interval: float = 0.4):
    """Poll the XU controls and print changes with timestamps (READ ONLY).

    Run this, then drive JMStudio (start a scan so the projector turns on). The
    selector that steps to a new stable value when the projector fires -- and
    back when it stops -- is the projector control. Reading works while JMStudio
    streams; property access is not blocked by streaming.
    """
    import time

    print(f"Watching XU on '{name_sub}' for {seconds:.0f}s "
          f"(READ ONLY). Start a scan in JMStudio now.\n")
    t0 = time.time()
    prev = {}
    seen = {}          # selector -> set of values
    changes = 0
    while time.time() - t0 < seconds:
        try:
            info = probe(name_sub)
        except Exception as exc:
            print(f"  [{time.time()-t0:6.1f}s] read failed: {exc}")
            time.sleep(interval)
            continue
        for c in info["controls"]:
            sel, val = c["selector"], c["value_hex"]
            seen.setdefault(sel, set()).add(val)
            if sel in prev and prev[sel] != val:
                print(f"  [{time.time()-t0:6.1f}s] selector {sel}: {prev[sel]} -> {val}")
                changes += 1
            prev[sel] = val
        time.sleep(interval)

    print(f"\n{changes} change(s) in {seconds:.0f}s. Distinct values per selector:")
    for sel in sorted(seen):
        vals = seen[sel]
        tag = "volatile/jitter" if len(vals) > 6 else ("stable" if len(vals) == 1 else "stepped")
        print(f"  selector {sel}: {len(vals)} value(s)  [{tag}]")
        if 1 < len(vals) <= 6:
            for v in sorted(vals):
                print(f"      {v}")
    print("\nA selector that is normally 'stable' but shows a few discrete values "
          "correlated with the scan starting/stopping is the projector control.")


def report(name_sub: str = "JMM") -> str:
    info = probe(name_sub)
    lines = [
        "Fox extension-unit control map (READ ONLY)",
        f"  camera : {info['filter']}",
        f"  XU     : {info['guid']}  (topology node {info['node']})",
        "",
    ]
    for c in info["controls"]:
        lines.append(f"  selector {c['selector']}: {c['access']:<7} "
                     f"len={c['length']}  cur={c['value_hex']}")
    lines += [
        "",
        "Access/length/current only -- selector semantics are not confirmed.",
        "Mapping a selector to projector power needs a USBPcap capture of JMStudio",
        "or a bounded write test (a device write, not performed here).",
    ]
    return "\n".join(lines)
