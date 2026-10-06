"""FoxStudio Desktop main window.

Left: project list + New/Import. Center: tabs (3D Model | Scan with the Fox).
Right: project details + pipeline actions. Bottom: activity log.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from .. import meshio
from ..project import Project
from .scanpanel import ScanPanel
from .viewer3d import Viewer3D
from .workers import TaskRunner


class NewProjectDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        form = QFormLayout(self)
        self.part = QLineEdit()
        self.customer = QLineEdit()
        self.vehicle = QLineEdit()
        self.notes = QLineEdit()
        form.addRow("Part name *", self.part)
        form.addRow("Customer", self.customer)
        form.addRow("Vehicle / machine", self.vehicle)
        form.addRow("Notes", self.notes)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FoxStudio Desktop")
        self.resize(1500, 900)
        self.runner = TaskRunner(self)
        self._projects: list[Project] = []

        # --- left: projects
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("Projects"))
        self.project_list = QListWidget()
        self.project_list.currentRowChanged.connect(self._on_select)
        lv.addWidget(self.project_list)
        btn_new = QPushButton("New Project...")
        btn_new.clicked.connect(self.new_project)
        lv.addWidget(btn_new)
        btn_import = QPushButton("Import scans/photos...")
        btn_import.clicked.connect(self.import_files)
        lv.addWidget(btn_import)

        # --- center: tabs
        self.viewer = Viewer3D()
        self.scan_panel = ScanPanel(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.viewer, "3D Model")
        self.tabs.addTab(self.scan_panel, "Scan with the Fox")

        # --- right: details + actions
        right = QWidget()
        rv = QVBoxLayout(right)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        rv.addWidget(self.details, stretch=1)
        for label, handler in [
            ("Measure", self.act_measure),
            ("Clean all scans", self.act_clean),
            ("Fuse && Mesh", self.act_mesh),
            ("Export STL + report + preview", self.act_export),
            ("Build printable package", self.act_package),
            ("Publish to parts library", self.act_publish),
        ]:
            b = QPushButton(label)
            b.clicked.connect(handler)
            rv.addWidget(b)

        # --- bottom: log
        self.log_pane = QPlainTextEdit()
        self.log_pane.setReadOnly(True)
        self.log_pane.setMaximumHeight(140)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.addWidget(right)
        split.setSizes([260, 860, 330])

        central = QWidget()
        cv = QVBoxLayout(central)
        cv.addWidget(split, stretch=1)
        cv.addWidget(self.log_pane)
        self.setCentralWidget(central)

        self.refresh_projects()
        self.log("Ready. Create a project, then import JMStudio exports or scan "
                 "natively on the 'Scan with the Fox' tab.")

    # -- helpers -----------------------------------------------------------
    def log(self, text: str):
        self.log_pane.appendPlainText(text)

    def run_task(self, job, on_done):
        started = self.runner.run(job, on_done, self.log,
                                  lambda err: self.log(f"ERROR: {err}"))
        if not started:
            self.log("Busy -- wait for the current task to finish.")

    def current_project(self):
        row = self.project_list.currentRow()
        if 0 <= row < len(self._projects):
            return self._projects[row]
        return None

    def refresh_projects(self, keep_selection: bool = False):
        row = self.project_list.currentRow()
        self._projects = Project.list_all()
        self.project_list.blockSignals(True)
        self.project_list.clear()
        for proj in self._projects:
            self.project_list.addItem(proj.meta.get("slug", proj.path.name))
        self.project_list.blockSignals(False)
        if keep_selection and 0 <= row < len(self._projects):
            self.project_list.setCurrentRow(row)
        elif self._projects:
            self.project_list.setCurrentRow(len(self._projects) - 1)

    def show_in_viewer(self, geom, caption: str = ""):
        desc = self.viewer.show_geometry(geom)
        self.tabs.setCurrentWidget(self.viewer)
        if caption:
            self.statusBar().showMessage(caption)
        else:
            self.statusBar().showMessage(desc)

    # -- project selection / details ----------------------------------------
    def _on_select(self, row: int):
        proj = self.current_project()
        if proj is None:
            self.details.clear()
            return
        m = proj.meta
        lines = [
            f"<h3>{m.get('part', '')}</h3>",
            f"<b>Customer:</b> {m.get('customer', '')}<br>",
            f"<b>Vehicle:</b> {m.get('vehicle', '') or '-'}<br>",
            f"<b>Created:</b> {m.get('created', '')}<br>",
            f"<b>Notes:</b> {m.get('notes', '') or '-'}<br>",
        ]
        meas = m.get("measurements", {})
        bb = meas.get("bbox_mm")
        if bb:
            lines.append(f"<b>Size:</b> {bb['x']} × {bb['y']} × {bb['z']} mm<br>")
        if "volume_cm3" in meas:
            lines.append(f"<b>Volume:</b> {meas['volume_cm3']} cm³ (watertight)<br>")
        elif "hull_volume_cm3" in meas:
            lines.append(f"<b>Volume (est):</b> {meas['hull_volume_cm3']} cm³<br>")
        if "scale_warning" in meas:
            lines.append(f"<b style='color:#c66'>{meas['scale_warning']}</b><br>")
        pub = m.get("published")
        if pub:
            lines.append(f"<b>Published:</b> post {pub.get('post_id')} ({pub.get('status')})<br>")
        for sub in ("raw", "cleaned", "export", "photos"):
            files = [f.name for f in sorted((proj.path / sub).glob("*"))]
            if files:
                lines.append(f"<br><b>{sub}/</b><br>" + "<br>".join(files))
        self.details.setHtml("".join(lines))
        try:
            src = proj.latest_geometry()
            geom = meshio.load(src)
            self.show_in_viewer(geom, f"{src.name} - {meshio.describe(geom)}")
        except (FileNotFoundError, ValueError):
            self.viewer.clear()
            self.statusBar().showMessage("No geometry yet - scan or import")

    # -- actions -------------------------------------------------------------
    def new_project(self):
        dlg = NewProjectDialog(self)
        if dlg.exec() != QDialog.Accepted or not dlg.part.text().strip():
            return
        proj = Project.create(dlg.part.text().strip(),
                              dlg.customer.text().strip() or None,
                              dlg.notes.text().strip(),
                              vehicle=dlg.vehicle.text().strip())
        self.log(f"Created project {proj.meta['slug']}")
        self.refresh_projects()

    def import_files(self):
        proj = self._need_project()
        if proj is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import scans and photos", "",
            "Scans & photos (*.stl *.obj *.ply *.asc *.xyz *.pts *.rscan "
            "*.jpg *.jpeg *.png *.webp);;All files (*)")
        if not paths:
            return
        from ..project import PHOTO_EXTS, PLACEHOLDER_EXTS
        for p in paths:
            p = Path(p)
            if p.suffix.lower() in PHOTO_EXTS:
                proj.import_photo(p)
                self.log(f"Photo: {p.name}")
            else:
                dest, role = proj.import_file(p)
                if role == "placeholder":
                    self.log(f"Stored {dest.name} as placeholder (export STL/PLY "
                             "from JMStudio to process it)")
                else:
                    self.log(f"Imported {dest.name}")
        self.refresh_projects(keep_selection=True)
        self._on_select(self.project_list.currentRow())

    def _need_project(self):
        proj = self.current_project()
        if proj is None:
            QMessageBox.information(self, "No project", "Create or select a project first.")
        return proj

    def act_measure(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            from ..measure import measure, format_report
            src = proj.latest_geometry()
            geom = meshio.load(src)
            result = measure(geom)
            proj.meta["measurements"] = result
            proj.save()
            log(format_report(result, title=src.name))
            return geom

        self.run_task(job, lambda geom: self._on_select(self.project_list.currentRow()))

    def act_clean(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            from ..cleanup import clean
            for src in proj.geometry_files(prefer_cleaned=False):
                geom = meshio.load(src)
                log(f"Cleaning {src.name} ({meshio.describe(geom)}) ...")
                geom, steps = clean(geom)
                for s in steps:
                    log(f"  - {s}")
                suffix = src.suffix if src.suffix.lower() not in (".asc", ".xyz", ".pts") else ".ply"
                out = proj.path / "cleaned" / f"{src.stem}_clean{suffix}"
                meshio.save(geom, out)
                log(f"  saved {out.name}")
            return None

        self.run_task(job, lambda _: self._on_select(self.project_list.currentRow()))

    def act_mesh(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            import open3d as o3d
            from ..registration import fuse, poisson_mesh
            clouds = []
            for f in [f for f in proj.geometry_files() if "_meshed" not in f.stem]:
                g = meshio.load(f)
                if isinstance(g, o3d.geometry.TriangleMesh):
                    g = g.sample_points_uniformly(200_000)
                clouds.append(g)
                log(f"  using {f.name}")
            if not clouds:
                raise ValueError("No geometry in this project yet.")
            log(f"Registering + fusing {len(clouds)} cloud(s) ...")
            fused = fuse(clouds, voxel=1.0)
            log(f"Poisson meshing {len(fused.points):,} points ...")
            mesh = poisson_mesh(fused, depth=9)
            out = proj.path / "cleaned" / f"{proj.meta['slug']}_meshed.ply"
            meshio.save(mesh, out)
            log(f"Saved {out.name} ({meshio.describe(mesh)})")
            return mesh

        self.run_task(job, lambda mesh: self.show_in_viewer(mesh))

    def act_export(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            import open3d as o3d
            from ..measure import measure
            from ..render import render_preview
            import json as _json
            src = proj.latest_geometry()
            geom = meshio.load(src)
            if isinstance(geom, o3d.geometry.PointCloud):
                raise ValueError("Latest geometry is a point cloud - run Fuse & Mesh first.")
            out = proj.path / "export" / f"{proj.meta['slug']}.stl"
            meshio.save(geom, out)
            log(f"Exported {out.name}")
            result = measure(geom)
            proj.meta["measurements"] = result
            proj.save()
            report = {k: proj.meta.get(k) for k in
                      ("slug", "part", "customer", "vehicle", "notes", "units",
                       "created", "updated", "files")}
            report["measurements"] = result
            (proj.path / "export" / "report.json").write_text(
                _json.dumps(report, indent=2), encoding="utf-8")
            render_preview(geom, proj.path / "export" / "preview.png")
            log("Wrote report.json and preview.png")
            return None

        self.run_task(job, lambda _: self._on_select(self.project_list.currentRow()))

    def act_package(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            from ..cli import cmd_package
            import argparse
            args = argparse.Namespace(target=str(proj.path), format="stl", scale=1.0)
            cmd_package(args)
            log(f"Printable folder ready: {proj.path / 'export'}")
            return None

        self.run_task(job, lambda _: self._on_select(self.project_list.currentRow()))

    def act_publish(self):
        proj = self._need_project()
        if proj is None:
            return

        def job(log):
            from ..wordpress import publish_or_queue
            result = publish_or_queue(proj)
            if "queued" in result:
                log(f"Site unreachable - queued: {result['queued']}")
            else:
                log(f"Published post {result['post_id']} ({result['status']}): {result.get('link')}")
                for m in result.get("media", []):
                    if "skipped" in m:
                        log(f"  media skipped: {m['skipped']} ({m['reason']})")
            return None

        self.run_task(job, lambda _: self._on_select(self.project_list.currentRow()))

    def closeEvent(self, event):
        self.scan_panel.closeEvent(event)
        super().closeEvent(event)
