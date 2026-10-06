"""Measurements: bounding boxes, volume, surface area.

Volume needs a watertight mesh; when the mesh has holes (typical for raw
scans) we report the convex-hull volume as an upper-bound estimate and say so.
Units follow the scan (JMStudio exports millimetres).
"""
from __future__ import annotations

import numpy as np
import open3d as o3d
import trimesh

from .meshio import Geometry


def _to_trimesh(mesh: o3d.geometry.TriangleMesh) -> trimesh.Trimesh:
    # process=True merges duplicated vertices; STL stores each triangle
    # independently, so without merging no STL would ever test watertight.
    return trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices),
        faces=np.asarray(mesh.triangles),
        process=True,
    )


def measure(geom: Geometry) -> dict:
    result: dict = {}

    aabb = geom.get_axis_aligned_bounding_box()
    extent = aabb.get_extent()
    result["bbox_mm"] = {
        "x": round(float(extent[0]), 3),
        "y": round(float(extent[1]), 3),
        "z": round(float(extent[2]), 3),
        "min": [round(float(v), 3) for v in aabb.min_bound],
        "max": [round(float(v), 3) for v in aabb.max_bound],
    }
    # Scale sanity: JMStudio exports mm; flag dimensions that look like the
    # wrong unit was assumed somewhere along the way.
    max_dim = float(max(extent))
    if max_dim < 2.0:
        result["scale_warning"] = (f"largest dimension is {max_dim:.3f} mm -- "
                                   "suspiciously small; was this exported in metres?")
    elif max_dim > 2000.0:
        result["scale_warning"] = (f"largest dimension is {max_dim:.0f} mm -- "
                                   "suspiciously large; check export units")

    try:
        obb = geom.get_oriented_bounding_box()
        result["oriented_bbox_mm"] = sorted(
            (round(float(v), 3) for v in obb.extent), reverse=True)
    except RuntimeError:
        pass  # degenerate geometry (e.g. coplanar points)

    if isinstance(geom, o3d.geometry.TriangleMesh):
        result["vertices"] = len(geom.vertices)
        result["triangles"] = len(geom.triangles)
        result["surface_area_mm2"] = round(float(geom.get_surface_area()), 2)

        tm = _to_trimesh(geom)
        result["watertight"] = bool(tm.is_watertight)
        if tm.is_watertight:
            result["volume_mm3"] = round(float(abs(tm.volume)), 2)
            result["volume_cm3"] = round(float(abs(tm.volume)) / 1000.0, 3)
        else:
            hull = tm.convex_hull
            result["volume_note"] = ("mesh is not watertight; convex-hull volume "
                                     "reported as an upper-bound estimate")
            result["hull_volume_mm3"] = round(float(hull.volume), 2)
            result["hull_volume_cm3"] = round(float(hull.volume) / 1000.0, 3)
    else:
        result["points"] = len(geom.points)
        try:
            hull, _ = geom.compute_convex_hull()
            hull_tm = _to_trimesh(hull)
            result["volume_note"] = "point cloud; convex-hull volume is an estimate"
            result["hull_volume_mm3"] = round(float(abs(hull_tm.volume)), 2)
            result["hull_volume_cm3"] = round(float(abs(hull_tm.volume)) / 1000.0, 3)
        except (RuntimeError, ValueError):
            pass

    return result


def format_report(result: dict, title: str = "") -> str:
    lines = []
    if title:
        lines.append(title)
    bb = result.get("bbox_mm", {})
    lines.append(f"  Bounding box : {bb.get('x')} x {bb.get('y')} x {bb.get('z')} mm")
    if "oriented_bbox_mm" in result:
        o = result["oriented_bbox_mm"]
        lines.append(f"  Oriented box : {o[0]} x {o[1]} x {o[2]} mm (fit)")
    if "surface_area_mm2" in result:
        lines.append(f"  Surface area : {result['surface_area_mm2']:,} mm^2")
    if "volume_mm3" in result:
        lines.append(f"  Volume       : {result['volume_mm3']:,} mm^3 "
                     f"({result['volume_cm3']} cm^3, watertight)")
    elif "hull_volume_mm3" in result:
        lines.append(f"  Hull volume  : {result['hull_volume_mm3']:,} mm^3 "
                     f"({result['hull_volume_cm3']} cm^3) -- {result['volume_note']}")
    if "triangles" in result:
        lines.append(f"  Geometry     : {result['vertices']:,} vertices, "
                     f"{result['triangles']:,} triangles"
                     + ("" if result.get("watertight") else " (not watertight)"))
    if "points" in result:
        lines.append(f"  Geometry     : {result['points']:,} points")
    if "scale_warning" in result:
        lines.append(f"  WARNING      : {result['scale_warning']}")
    return "\n".join(lines)
