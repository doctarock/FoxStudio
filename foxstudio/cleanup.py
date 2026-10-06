"""Mesh and point-cloud cleanup helpers built on Open3D (+ trimesh for hole filling)."""
from __future__ import annotations

import numpy as np
import open3d as o3d
import trimesh

from .meshio import Geometry


def clean_point_cloud(
    pcd: o3d.geometry.PointCloud,
    voxel: float = 0.0,
    stat_neighbors: int = 20,
    stat_std: float = 2.0,
    radius: float = 0.0,
    radius_min_points: int = 16,
) -> tuple[o3d.geometry.PointCloud, list[str]]:
    """Downsample and strip outliers. Distances are in scan units (mm)."""
    log = []
    n0 = len(pcd.points)
    if voxel > 0:
        pcd = pcd.voxel_down_sample(voxel)
        log.append(f"voxel downsample @ {voxel} mm: {n0:,} -> {len(pcd.points):,} points")
    n = len(pcd.points)
    if stat_neighbors > 0:
        pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=stat_neighbors, std_ratio=stat_std)
        log.append(f"statistical outlier removal: dropped {n - len(pcd.points):,} points")
    n = len(pcd.points)
    if radius > 0:
        pcd, _ = pcd.remove_radius_outlier(nb_points=radius_min_points, radius=radius)
        log.append(f"radius outlier removal (r={radius} mm): dropped {n - len(pcd.points):,} points")
    if not pcd.has_normals():
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=3.0, max_nn=30))
        pcd.orient_normals_consistent_tangent_plane(k=15)
        log.append("estimated and oriented normals")
    return pcd, log


def clean_mesh(
    mesh: o3d.geometry.TriangleMesh,
    min_component_ratio: float = 0.01,
    smooth_iterations: int = 0,
    fill_holes: bool = True,
    target_triangles: int = 0,
) -> tuple[o3d.geometry.TriangleMesh, list[str]]:
    """Standard scan-mesh cleanup: degenerate/duplicate removal, island removal,
    optional hole filling, Taubin smoothing, and decimation."""
    log = []
    n0 = len(mesh.triangles)

    mesh.remove_duplicated_vertices()
    mesh.remove_duplicated_triangles()
    mesh.remove_degenerate_triangles()
    mesh.remove_non_manifold_edges()
    mesh.remove_unreferenced_vertices()
    if len(mesh.triangles) != n0:
        log.append(f"removed {n0 - len(mesh.triangles):,} duplicate/degenerate/non-manifold triangles")

    # Drop small disconnected islands (scan debris, turntable fragments).
    if min_component_ratio > 0:
        tri_clusters, cluster_sizes, _ = mesh.cluster_connected_triangles()
        tri_clusters = np.asarray(tri_clusters)
        cluster_sizes = np.asarray(cluster_sizes)
        if len(cluster_sizes) > 1:
            keep = cluster_sizes >= max(1, int(cluster_sizes.max() * min_component_ratio))
            mask = ~keep[tri_clusters]
            mesh.remove_triangles_by_mask(mask)
            mesh.remove_unreferenced_vertices()
            log.append(f"removed {int((~keep).sum())} small island(s), "
                       f"{int(mask.sum()):,} triangles")

    # Reorient triangles so face windings (and therefore normals) are consistent.
    tm = trimesh.Trimesh(vertices=np.asarray(mesh.vertices),
                         faces=np.asarray(mesh.triangles), process=False)
    trimesh.repair.fix_normals(tm)
    if fill_holes and not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
        trimesh.repair.fix_normals(tm)
        if tm.is_watertight:
            log.append("filled holes -> mesh is now watertight")
        else:
            log.append("filled small holes (larger openings remain; try 'foxstudio mesh' "
                       "for a full Poisson rebuild)")
    log.append("reoriented normals consistently")
    mesh = o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(tm.vertices),
        o3d.utility.Vector3iVector(tm.faces))

    if smooth_iterations > 0:
        mesh = mesh.filter_smooth_taubin(number_of_iterations=smooth_iterations)
        log.append(f"Taubin smoothing x{smooth_iterations}")

    if target_triangles > 0 and len(mesh.triangles) > target_triangles:
        mesh = mesh.simplify_quadric_decimation(target_number_of_triangles=target_triangles)
        log.append(f"decimated to {len(mesh.triangles):,} triangles")

    mesh.compute_vertex_normals()
    return mesh, log


def clean(geom: Geometry, **kwargs) -> tuple[Geometry, list[str]]:
    if isinstance(geom, o3d.geometry.TriangleMesh):
        keys = ("min_component_ratio", "smooth_iterations", "fill_holes", "target_triangles")
    else:
        keys = ("voxel", "stat_neighbors", "stat_std", "radius", "radius_min_points")
    opts = {k: v for k, v in kwargs.items() if k in keys and v is not None}
    if isinstance(geom, o3d.geometry.TriangleMesh):
        return clean_mesh(geom, **opts)
    return clean_point_cloud(geom, **opts)
