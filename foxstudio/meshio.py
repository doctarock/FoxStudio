"""Load and save scan geometry: STL, OBJ, PLY (mesh or point cloud), ASC.

Everything is normalized to Open3D objects: TriangleMesh when faces exist,
PointCloud otherwise. JMStudio ASC exports are plain text rows of
``x y z [nx ny nz] [r g b]`` (space or comma separated).
"""
from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np
import open3d as o3d

Geometry = Union[o3d.geometry.TriangleMesh, o3d.geometry.PointCloud]

MESH_EXTS = {".stl", ".obj"}
EITHER_EXTS = {".ply"}
CLOUD_EXTS = {".asc", ".xyz", ".pts"}
ALL_EXTS = MESH_EXTS | EITHER_EXTS | CLOUD_EXTS


def load_asc(path: Path) -> o3d.geometry.PointCloud:
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip().replace(",", " ")
            if not line or line.startswith(("#", "//", ";")):
                continue
            parts = line.split()
            try:
                rows.append([float(v) for v in parts])
            except ValueError:
                continue  # header or junk line
    if not rows:
        raise ValueError(f"No point data found in {path}")
    width = min(len(r) for r in rows)
    data = np.array([r[:width] for r in rows], dtype=np.float64)
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(data[:, 0:3])
    if width >= 6:
        block = data[:, 3:6]
        # Heuristic: unit-length rows are normals; 0-255 or 0-1 rows are colors.
        norms = np.linalg.norm(block, axis=1)
        if np.allclose(norms, 1.0, atol=0.1):
            pcd.normals = o3d.utility.Vector3dVector(block)
        else:
            colors = block / 255.0 if block.max() > 1.0 else block
            pcd.colors = o3d.utility.Vector3dVector(np.clip(colors, 0, 1))
    if width >= 9:
        colors = data[:, 6:9]
        colors = colors / 255.0 if colors.max() > 1.0 else colors
        pcd.colors = o3d.utility.Vector3dVector(np.clip(colors, 0, 1))
    return pcd


def load(path: Union[str, Path]) -> Geometry:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in ALL_EXTS:
        raise ValueError(f"Unsupported format '{ext}' (supported: {', '.join(sorted(ALL_EXTS))})")
    if ext in CLOUD_EXTS:
        return load_asc(path)
    mesh = o3d.io.read_triangle_mesh(str(path))
    if len(mesh.triangles) > 0:
        if not mesh.has_vertex_normals():
            mesh.compute_vertex_normals()
        return mesh
    # PLY (or a degenerate mesh file) holding only points
    pcd = o3d.io.read_point_cloud(str(path))
    if len(pcd.points) == 0:
        raise ValueError(f"No geometry found in {path}")
    return pcd


def save(geom: Geometry, path: Union[str, Path]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    if isinstance(geom, o3d.geometry.TriangleMesh):
        if ext == ".stl" and not geom.has_triangle_normals():
            geom.compute_triangle_normals()
        if not o3d.io.write_triangle_mesh(str(path), geom):
            raise IOError(f"Failed to write {path}")
    elif isinstance(geom, o3d.geometry.PointCloud):
        if ext in MESH_EXTS:
            raise ValueError(f"Cannot save a point cloud as {ext}; mesh it first "
                             "(foxstudio mesh) or save as .ply/.asc")
        if ext in CLOUD_EXTS:
            _save_asc(geom, path)
        elif not o3d.io.write_point_cloud(str(path), geom):
            raise IOError(f"Failed to write {path}")
    else:
        raise TypeError(f"Unsupported geometry type: {type(geom)}")
    return path


def _save_asc(pcd: o3d.geometry.PointCloud, path: Path) -> None:
    pts = np.asarray(pcd.points)
    cols = [pts]
    if pcd.has_normals():
        cols.append(np.asarray(pcd.normals))
    if pcd.has_colors():
        cols.append(np.asarray(pcd.colors) * 255.0)
    np.savetxt(path, np.hstack(cols), fmt="%.6f")


def describe(geom: Geometry) -> str:
    if isinstance(geom, o3d.geometry.TriangleMesh):
        return f"mesh ({len(geom.vertices):,} vertices, {len(geom.triangles):,} triangles)"
    return f"point cloud ({len(geom.points):,} points)"
