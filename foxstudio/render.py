"""Preview image rendering for reports and the WordPress parts library.

Tries Open3D's offscreen renderer first; if that fails (no GL context, headless
box), falls back to a dependency-free normal-shaded splat render via numpy+cv2.
Always produces a PNG.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import open3d as o3d

from .meshio import Geometry

SIZE = 1024


def _open3d_render(geom: Geometry, out: Path) -> bool:
    try:
        from open3d.visualization import rendering
        renderer = rendering.OffscreenRenderer(SIZE, SIZE)
        mat = rendering.MaterialRecord()
        mat.shader = "defaultLit"
        mat.base_color = (0.72, 0.75, 0.78, 1.0)
        renderer.scene.set_background([1.0, 1.0, 1.0, 1.0])
        renderer.scene.add_geometry("part", geom, mat)
        bounds = geom.get_axis_aligned_bounding_box()
        renderer.setup_camera(50.0, bounds, bounds.get_center())
        renderer.scene.scene.set_sun_light([-0.4, -0.6, -1.0], [1.0, 1.0, 1.0], 90000)
        renderer.scene.scene.enable_sun_light(True)
        img = renderer.render_to_image()
        return bool(o3d.io.write_image(str(out), img))
    except Exception:
        return False


def _splat_render(geom: Geometry, out: Path) -> None:
    """Orthographic 3/4-view splat render shaded by surface normal."""
    if isinstance(geom, o3d.geometry.TriangleMesh):
        pcd = geom.sample_points_uniformly(min(300_000, max(50_000, len(geom.triangles) * 3)))
    else:
        pcd = geom
        if not pcd.has_normals():
            pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=3.0, max_nn=30))

    pts = np.asarray(pcd.points).copy()
    normals = np.asarray(pcd.normals).copy()

    # rotate to a 3/4 isometric-ish view
    rot = o3d.geometry.get_rotation_matrix_from_xyz((np.deg2rad(-60), 0, np.deg2rad(25)))
    pts = pts @ rot.T
    normals = normals @ rot.T

    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = float(max(hi[0] - lo[0], hi[1] - lo[1])) or 1.0
    margin = 60
    scale = (SIZE - 2 * margin) / span
    xy = ((pts[:, :2] - lo[:2]) * scale + margin).astype(int)
    xy[:, 1] = SIZE - 1 - xy[:, 1]  # image y is down

    order = np.argsort(pts[:, 2])  # painter's algorithm, back to front
    shade = np.clip(np.abs(normals[:, 2]) * 0.75 + 0.2, 0, 1)

    img = np.full((SIZE, SIZE, 3), 255, np.uint8)
    base = np.array([198, 172, 155], np.float64)  # BGR steel-blue-grey
    r = max(1, int(scale * 0.4))
    for i in order:
        x, y = xy[i]
        if 0 <= x < SIZE and 0 <= y < SIZE:
            cv2.circle(img, (int(x), int(y)), r, (base * shade[i]).tolist(), -1)
    cv2.imwrite(str(out), img)


def render_preview(geom: Geometry, out: Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not _open3d_render(geom, out):
        _splat_render(geom, out)
    return out
