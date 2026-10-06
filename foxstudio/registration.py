"""Point-cloud registration, fusion, and meshing with Open3D.

Pipeline for multiple partial scans of the same part:
  1. global init per pair (FPFH features + RANSAC)
  2. refine with point-to-plane ICP
  3. multiway pose-graph optimization
  4. fuse into one cloud, then Poisson surface reconstruction
  5. trim low-density Poisson bubbles

All distances are in scan units (mm for JMStudio exports).
"""
from __future__ import annotations

import numpy as np
import open3d as o3d


def _prep(pcd: o3d.geometry.PointCloud, voxel: float):
    down = pcd.voxel_down_sample(voxel)
    down.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
    fpfh = o3d.pipelines.registration.compute_fpfh_feature(
        down, o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 5, max_nn=100))
    return down, fpfh


def pairwise_register(
    source: o3d.geometry.PointCloud,
    target: o3d.geometry.PointCloud,
    voxel: float = 1.0,
) -> np.ndarray:
    """Transform mapping source onto target: RANSAC global init + point-to-plane ICP."""
    src_down, src_fpfh = _prep(source, voxel)
    tgt_down, tgt_fpfh = _prep(target, voxel)

    dist = voxel * 1.5
    ransac = o3d.pipelines.registration.registration_ransac_based_on_feature_matching(
        src_down, tgt_down, src_fpfh, tgt_fpfh, mutual_filter=True,
        max_correspondence_distance=dist,
        estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(False),
        ransac_n=3,
        checkers=[
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnEdgeLength(0.9),
            o3d.pipelines.registration.CorrespondenceCheckerBasedOnDistance(dist),
        ],
        criteria=o3d.pipelines.registration.RANSACConvergenceCriteria(100000, 0.999))

    icp = o3d.pipelines.registration.registration_icp(
        src_down, tgt_down, voxel, ransac.transformation,
        o3d.pipelines.registration.TransformationEstimationPointToPlane())
    return icp.transformation


def multiway_register(
    clouds: list[o3d.geometry.PointCloud],
    voxel: float = 1.0,
) -> list[np.ndarray]:
    """Register every cloud into the frame of clouds[0] via pose-graph optimization."""
    downs = []
    for pcd in clouds:
        d = pcd.voxel_down_sample(voxel)
        d.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 2, max_nn=30))
        downs.append(d)

    pose_graph = o3d.pipelines.registration.PoseGraph()
    pose_graph.nodes.append(o3d.pipelines.registration.PoseGraphNode(np.identity(4)))
    odometry = np.identity(4)
    dist = voxel * 1.5

    for i in range(len(downs)):
        for j in range(i + 1, len(downs)):
            trans = pairwise_register(clouds[i], clouds[j], voxel)
            info = o3d.pipelines.registration.get_information_matrix_from_point_clouds(
                downs[i], downs[j], dist, trans)
            if j == i + 1:  # odometry edge
                odometry = trans @ odometry
                pose_graph.nodes.append(
                    o3d.pipelines.registration.PoseGraphNode(np.linalg.inv(odometry)))
                pose_graph.edges.append(o3d.pipelines.registration.PoseGraphEdge(
                    i, j, trans, info, uncertain=False))
            else:  # loop closure
                pose_graph.edges.append(o3d.pipelines.registration.PoseGraphEdge(
                    i, j, trans, info, uncertain=True))

    option = o3d.pipelines.registration.GlobalOptimizationOption(
        max_correspondence_distance=dist,
        edge_prune_threshold=0.25,
        reference_node=0)
    o3d.pipelines.registration.global_optimization(
        pose_graph,
        o3d.pipelines.registration.GlobalOptimizationLevenbergMarquardt(),
        o3d.pipelines.registration.GlobalOptimizationConvergenceCriteria(),
        option)
    return [np.asarray(node.pose) for node in pose_graph.nodes]


def fuse(
    clouds: list[o3d.geometry.PointCloud],
    voxel: float = 1.0,
) -> o3d.geometry.PointCloud:
    """Register (if more than one cloud) and merge into a single downsampled cloud."""
    if len(clouds) == 1:
        merged = clouds[0]
    else:
        poses = multiway_register(clouds, voxel)
        merged = o3d.geometry.PointCloud()
        for pcd, pose in zip(clouds, poses):
            merged += o3d.geometry.PointCloud(pcd).transform(pose)
        merged = merged.voxel_down_sample(voxel * 0.5)
    if not merged.has_normals():
        merged.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=voxel * 3, max_nn=30))
        merged.orient_normals_consistent_tangent_plane(k=15)
    return merged


def poisson_mesh(
    pcd: o3d.geometry.PointCloud,
    depth: int = 9,
    density_quantile: float = 0.02,
) -> o3d.geometry.TriangleMesh:
    """Poisson reconstruction with low-density trim (removes closure bubbles)."""
    if not pcd.has_normals():
        pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=3.0, max_nn=30))
        pcd.orient_normals_consistent_tangent_plane(k=15)
    mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=depth)
    densities = np.asarray(densities)
    mesh.remove_vertices_by_mask(densities < np.quantile(densities, density_quantile))
    mesh.remove_unreferenced_vertices()
    mesh.compute_vertex_normals()
    return mesh
