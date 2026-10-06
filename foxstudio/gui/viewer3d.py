"""3D model viewer built on pyqtgraph's GL view (orbit/zoom with the mouse)."""
from __future__ import annotations

import numpy as np
import open3d as o3d
import pyqtgraph.opengl as gl
from PySide6.QtGui import QVector3D
from PySide6.QtWidgets import QVBoxLayout, QWidget


class Viewer3D(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor((24, 26, 30))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self._items = []
        self._grid = gl.GLGridItem()
        self._grid.setSize(200, 200)
        self._grid.setSpacing(10, 10)
        self.view.addItem(self._grid)

    def clear(self):
        for item in self._items:
            self.view.removeItem(item)
        self._items = []

    def show_geometry(self, geom) -> str:
        """Display an Open3D mesh or point cloud. Returns a description."""
        self.clear()
        if isinstance(geom, o3d.geometry.TriangleMesh):
            verts = np.asarray(geom.vertices, dtype=np.float32)
            faces = np.asarray(geom.triangles, dtype=np.uint32)
            if not geom.has_vertex_normals():
                geom.compute_vertex_normals()
            md = gl.MeshData(vertexes=verts, faces=faces)
            item = gl.GLMeshItem(meshdata=md, smooth=True, shader="shaded",
                                 color=(0.62, 0.66, 0.72, 1.0), drawEdges=False)
            desc = f"{len(verts):,} vertices, {len(faces):,} triangles"
        else:
            pts = np.asarray(geom.points, dtype=np.float32)
            if geom.has_colors():
                colors = np.asarray(geom.colors, dtype=np.float32)
                colors = np.hstack([colors, np.ones((len(colors), 1), np.float32)])
            else:
                colors = np.tile((0.62, 0.72, 0.85, 1.0), (len(pts), 1)).astype(np.float32)
            step = max(1, len(pts) // 400_000)  # keep the GL scene responsive
            item = gl.GLScatterPlotItem(pos=pts[::step], color=colors[::step],
                                        size=2.0, pxMode=True)
            item.setGLOptions("opaque")
            desc = f"{len(pts):,} points"
        self.view.addItem(item)
        self._items.append(item)
        self._frame(geom)
        return desc

    def _frame(self, geom):
        bbox = geom.get_axis_aligned_bounding_box()
        center = bbox.get_center()
        extent = float(np.max(bbox.get_extent())) or 100.0
        self._grid.resetTransform()
        self._grid.setSize(extent * 2, extent * 2)
        self._grid.setSpacing(extent / 10, extent / 10)
        self._grid.translate(center[0], center[1], float(bbox.min_bound[2]))
        self.view.opts["center"] = QVector3D(*[float(c) for c in center])
        self.view.setCameraPosition(distance=extent * 2.2, elevation=25, azimuth=45)
