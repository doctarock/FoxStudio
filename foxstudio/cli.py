"""FoxStudio command-line interface.

    foxstudio new "impeller housing" --customer "Acme"      create a project
    foxstudio import scan.stl --part "bracket"              import + auto-create
    foxstudio list                                          show all projects
    foxstudio measure <project|file>                        bbox / volume / area
    foxstudio clean <project|file> [--voxel ...]            cleanup helpers
    foxstudio mesh <project> [--depth 9]                    clouds -> fused Poisson mesh
    foxstudio export <project> --format stl                 write export/<slug>.stl + report.json + preview
    foxstudio package <project>                             printable folder (mesh, report, preview, photos)
    foxstudio publish <project>                             push to WordPress (queues offline)
    foxstudio sync                                          flush the offline queue
    foxstudio probe                                         Fox USB investigation report
    foxstudio capture <project> [--pairs 12]                direct stereo capture
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _resolve_geometry(target: str):
    """Accept a project name or a bare geometry file path."""
    from . import meshio
    from .project import Project
    p = Path(target)
    if p.is_file() and p.suffix.lower() in meshio.ALL_EXTS:
        return meshio.load(p), p, None
    proj = Project.find(target)
    src = proj.latest_geometry()
    return meshio.load(src), src, proj


def _import_into(proj, scan_paths, photo_paths) -> int:
    from . import meshio
    from .project import PLACEHOLDER_EXTS, PHOTO_EXTS
    for f in scan_paths:
        f = Path(f)
        if not f.is_file():
            print(f"error: not a file: {f}", file=sys.stderr)
            return 1
        ext = f.suffix.lower()
        if ext in PHOTO_EXTS:
            photo_paths = list(photo_paths) + [f]
            continue
        if ext not in meshio.ALL_EXTS and ext not in PLACEHOLDER_EXTS:
            print(f"error: unsupported format: {f.name} "
                  f"(accepted: {', '.join(sorted(meshio.ALL_EXTS | PLACEHOLDER_EXTS))})",
                  file=sys.stderr)
            return 1
        dest, role = proj.import_file(f)
        if role == "placeholder":
            print(f"  stored {dest.name} as a placeholder (JMStudio project format; "
                  "export STL/OBJ/PLY/ASC from JMStudio to process it)")
        else:
            geom = meshio.load(dest)
            print(f"  imported {dest.name}: {meshio.describe(geom)}")
    for p in photo_paths:
        p = Path(p)
        if not p.is_file():
            print(f"error: not a file: {p}", file=sys.stderr)
            return 1
        dest = proj.import_photo(p)
        print(f"  photo {dest.name}")
    return 0


def cmd_new(args) -> int:
    from .project import Project
    proj = Project.create(args.part, args.customer, args.notes or "",
                          vehicle=args.vehicle or "")
    print(f"Created project: {proj.meta['slug']}")
    print(f"  {proj.path}")
    return _import_into(proj, args.scan or [], args.photo or [])


def cmd_import(args) -> int:
    from .project import Project
    files = [Path(f) for f in args.files]
    if args.project:
        proj = Project.find(args.project)
    else:
        part = args.part or files[0].stem
        proj = Project.create(part, args.customer, vehicle=args.vehicle or "")
        print(f"Created project: {proj.meta['slug']}")
    rc = _import_into(proj, files, args.photo or [])
    if rc != 0:
        return rc
    if args.measure:
        args.target = proj.meta["slug"]
        return cmd_measure(args)
    return 0


def cmd_list(args) -> int:
    from .project import Project
    projects = Project.list_all()
    if not projects:
        from .config import projects_dir
        print(f"No projects yet in {projects_dir()}")
        return 0
    for proj in projects:
        m = proj.meta
        raw = len(list((proj.path / "raw").glob("*")))
        exp = len(list((proj.path / "export").glob("*")))
        pub = "published" if m.get("published") else ""
        print(f"{m['slug']:<48} raw:{raw} export:{exp} {pub}")
    return 0


def cmd_measure(args) -> int:
    from .measure import measure, format_report
    geom, src, proj = _resolve_geometry(args.target)
    result = measure(geom)
    print(format_report(result, title=str(src.name)))
    if proj is not None:
        proj.meta["measurements"] = result
        proj.save()
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def cmd_clean(args) -> int:
    from . import meshio
    from .cleanup import clean
    from .project import Project

    p = Path(args.target)
    if p.is_file():
        targets, proj = [p], None
    else:
        proj = Project.find(args.target)
        targets = proj.geometry_files(prefer_cleaned=False)  # clean every raw scan
        if not targets:
            print("error: project has no geometry files", file=sys.stderr)
            return 1

    for src in targets:
        geom = meshio.load(src)
        print(f"Cleaning {src.name}: {meshio.describe(geom)}")
        geom, log = clean(
            geom,
            voxel=args.voxel,
            stat_neighbors=args.stat_neighbors,
            stat_std=args.stat_std,
            radius=args.radius,
            smooth_iterations=args.smooth,
            fill_holes=not args.no_fill_holes,
            target_triangles=args.decimate,
            min_component_ratio=args.min_island,
        )
        for line in log:
            print(f"  - {line}")
        suffix = src.suffix if src.suffix.lower() not in (".asc", ".xyz", ".pts") else ".ply"
        if proj is not None:
            out = proj.path / "cleaned" / f"{src.stem}_clean{suffix}"
        else:
            out = src.with_name(f"{src.stem}_clean{suffix}")
        meshio.save(geom, out)
        print(f"Saved: {out} ({meshio.describe(geom)})")
    return 0


def cmd_mesh(args) -> int:
    """Fuse a project's point clouds (registering multiple scans) and Poisson-mesh them."""
    from . import meshio
    from .project import Project
    from .registration import fuse, poisson_mesh
    import open3d as o3d

    proj = Project.find(args.target)
    clouds = []
    sources = []
    files = [f for f in proj.geometry_files() if "_meshed" not in f.stem]
    for f in files:
        g = meshio.load(f)
        if isinstance(g, o3d.geometry.TriangleMesh):
            # sample meshes so partial mesh scans can be aligned and merged too
            g = g.sample_points_uniformly(200_000)
        clouds.append(g)
        sources.append(f.name)
    if not clouds:
        print("error: no geometry found in this project. Import scans first.", file=sys.stderr)
        return 1

    print(f"Fusing {len(clouds)} cloud(s): {', '.join(sources)}")
    fused = fuse(clouds, voxel=args.voxel)
    print(f"  fused cloud: {len(fused.points):,} points")
    print(f"Poisson reconstruction (depth={args.depth}) ...")
    mesh = poisson_mesh(fused, depth=args.depth)
    out = proj.path / "cleaned" / f"{proj.meta['slug']}_meshed.ply"
    meshio.save(mesh, out)
    print(f"Saved: {out} ({meshio.describe(mesh)})")
    return 0


def _write_report(proj, geom) -> Path:
    """report.json: full metadata + fresh measurements, saved into export/."""
    from .measure import measure
    result = measure(geom)
    proj.meta["measurements"] = result
    proj.save()
    report = {k: proj.meta.get(k) for k in
              ("slug", "part", "customer", "vehicle", "notes", "units",
               "created", "updated", "files")}
    report["measurements"] = result
    out = proj.path / "export" / "report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


def cmd_export(args) -> int:
    from . import meshio
    from .render import render_preview
    import open3d as o3d
    geom, src, proj = _resolve_geometry(args.target)
    fmt = args.format.lower().lstrip(".")
    if isinstance(geom, o3d.geometry.PointCloud) and fmt in ("stl", "obj"):
        print("error: latest geometry is a point cloud; run 'foxstudio mesh' first.",
              file=sys.stderr)
        return 1
    if args.scale != 1.0:
        geom.scale(args.scale, center=(0, 0, 0))
        print(f"  scaled by {args.scale}")
    if proj is not None:
        out = proj.path / "export" / f"{proj.meta['slug']}.{fmt}"
    else:
        out = src.with_suffix(f".{fmt}")
    meshio.save(geom, out)
    print(f"Exported: {out}")
    if proj is not None:
        print(f"Report:   {_write_report(proj, geom)}")
        preview = render_preview(geom, proj.path / "export" / "preview.png")
        print(f"Preview:  {preview}")
    return 0


def cmd_package(args) -> int:
    """Printable project folder: cleaned mesh, report.json, preview, photos, notes."""
    import shutil
    from .project import Project
    proj = Project.find(args.target)

    if not list((proj.path / "export").glob("*.stl")) and \
       not list((proj.path / "export").glob("*.obj")):
        args.format, args.scale = "stl", 1.0
        rc = cmd_export(args)
        if rc != 0:
            return rc

    export_dir = proj.path / "export"
    for photo in sorted((proj.path / "photos").glob("*")):
        dest = export_dir / photo.name
        if not dest.exists():
            shutil.copy2(photo, dest)

    m = proj.meta
    notes = [
        f"Part:     {m.get('part', '')}",
        f"Customer: {m.get('customer', '')}",
    ]
    if m.get("vehicle"):
        notes.append(f"Vehicle:  {m['vehicle']}")
    notes += [f"Scanned:  {m.get('created', '')}", ""]
    if m.get("notes"):
        notes += [m["notes"], ""]
    bb = m.get("measurements", {}).get("bbox_mm")
    if bb:
        notes.append(f"Bounding box: {bb['x']} x {bb['y']} x {bb['z']} mm")
    (export_dir / "NOTES.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")

    print(f"Printable project folder: {export_dir}")
    for f in sorted(export_dir.glob("*")):
        print(f"  {f.name}")
    return 0


def cmd_publish(args) -> int:
    from .project import Project
    from .wordpress import build_payload, publish_or_queue, WordPressError
    proj = Project.find(args.target)
    if not list((proj.path / "export").glob("*")):
        print("note: no files in export/ yet -- publishing metadata only. "
              "Run 'foxstudio export' first to attach models.")
    terms = {k: v for k, v in {
        "part_category": args.category,
        "part_type": args.type,
        "part_material": args.material,
        "part_brand": args.brand,
    }.items() if v}
    if args.dry_run:
        print(json.dumps(build_payload(proj, terms), indent=2))
        return 0
    try:
        result = publish_or_queue(proj, terms)
    except WordPressError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if "queued" in result:
        print(f"Offline -- queued for later: {result['queued']}")
        print("Run 'foxstudio sync' when back online.")
    else:
        print(f"Published post {result['post_id']} ({result['status']}): {result.get('link')}")
        for m in result.get("media", []):
            if "skipped" in m:
                print(f"  media skipped: {m['skipped']} ({m['reason']})")
            else:
                print(f"  media: {m['file']} -> {m.get('url')}")
    return 0


def cmd_sync(args) -> int:
    from .wordpress import sync_queue, WordPressError
    try:
        results = sync_queue()
    except WordPressError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not results:
        print("Queue is empty.")
        return 0
    ok = sum(1 for r in results if r["ok"])
    for r in results:
        mark = "ok " if r["ok"] else "FAIL"
        detail = r.get("link") or r.get("error", "")
        print(f"  [{mark}] {r['entry']}  {detail}")
    print(f"{ok}/{len(results)} pushed.")
    return 0 if ok == len(results) else 1


def cmd_probe(args) -> int:
    from .capture.probe import report
    print(report())
    return 0


def cmd_xu(args) -> int:
    """Read-only map (or live watch) of the Fox's UVC extension-unit controls."""
    from .capture.xu import report, watch
    try:
        if args.watch:
            watch(args.camera, seconds=args.seconds)
        else:
            print(report(args.camera))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_turntable(args) -> int:
    """Connect to the Ciclop/Horus board and jog the table / test lasers."""
    from .turntable import Turntable, TurntableError, find_port
    try:
        tt = Turntable(port=args.port)
        banner = tt.connect()
    except TurntableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Connected on {tt.port}: {banner or '(no banner)'}")
    try:
        if args.laser_test:
            import time
            for i in (0, 1):
                print(f"  laser {i} on"); tt.laser(i, True); time.sleep(0.6); tt.laser(i, False)
        if args.rotate:
            print(f"  rotating {args.rotate} deg"); tt.rotate(args.rotate)
            print(f"  position: {tt.position} deg")
    finally:
        tt.disconnect()
    return 0


def cmd_turntable_calibrate(args) -> int:
    """Calibrate the turntable rotation axis using a checkerboard on the platform."""
    import cv2
    from . import fox_calibration
    from .stereo import Reconstructor
    from .laserscan import calibrate_axis_from_checkerboard
    from .turntable import Turntable, TurntableError
    from .capture.camera import find_fox_cameras, open_camera

    fox = fox_calibration.load()
    if fox is None:
        print("error: no factory calibration (run 'foxstudio calib')", file=sys.stderr)
        return 1
    recon = Reconstructor(fox.as_stereo())
    refs = find_fox_cameras()
    if not refs:
        print("error: Fox camera not found (close JMStudio)", file=sys.stderr)
        return 1
    try:
        tt = Turntable(port=args.port); tt.connect()
    except TurntableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cap = open_camera(refs[0])
    poses = []
    try:
        print(f"Place the checkerboard flat on the turntable. Capturing {args.views} views...")
        tt.reset_origin()
        step = 360.0 / args.views
        for k in range(args.views):
            import time; time.sleep(0.4)
            for _ in range(3):
                cap.grab()
            ok, frame = cap.retrieve()
            rect = cv2.remap(frame, recon.map1[0], recon.map1[1], cv2.INTER_LINEAR)
            poses.append((k * step, rect))
            print(f"  view {k+1}/{args.views} at {k*step:.0f} deg")
            tt.rotate(step)
    finally:
        cap.release(); tt.disable_motor(); tt.disconnect()
    try:
        axis = calibrate_axis_from_checkerboard(recon, poses)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    axis.save()
    print(f"Turntable axis saved: point={axis.point.round(1)} dir={axis.direction.round(3)}")
    return 0


def cmd_laser_aim(args) -> int:
    from .turntable import Turntable, TurntableError
    from .laserscan import laser_aim
    try:
        tt = Turntable(port=args.port); tt.connect()
    except TurntableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        laser_aim(tt, laser=args.laser, threshold=args.threshold, exposure=args.exposure)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        tt.disconnect()
    return 0


def cmd_laser_scan(args) -> int:
    from .project import Project
    from .turntable import Turntable, TurntableError
    from .laserscan import laser_scan
    proj = Project.find(args.target)
    try:
        tt = Turntable(port=args.port); tt.connect()
    except TurntableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        laser_scan(proj, tt, steps=args.steps, threshold=args.threshold)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        tt.disconnect()
    return 0


def cmd_calib(args) -> int:
    """Import/show the Fox factory calibration cached by JMStudio."""
    from . import fox_calibration
    fox = fox_calibration.load(args.serial)
    if fox is None:
        print("No factory calibration found. Run a scan in JMStudio once so it "
              "caches calib_<serial>.txt, or check the install path.", file=sys.stderr)
        return 1
    print(f"Fox factory calibration: {fox.serial}")
    print(f"  image size    : {fox.image_size[0]}x{fox.image_size[1]}")
    print(f"  camera baseline: {fox.baseline_mm:.2f} mm")
    print(f"  projector base : {float((fox.proj_T[0]**2+fox.proj_T[1]**2+fox.proj_T[2]**2)**0.5):.2f} mm")
    print(f"  projector LUT  : {len(fox.lut)} entries")
    print(f"  pattern        : {fox.pattern.shape[0]} rows x {fox.pattern.shape[1]} "
          f"(symbols {sorted(set(fox.pattern.flatten().tolist()))})")
    print(f"  calibrated     : {fox.meta.get('CalibrateDate', '?')} "
          f"({fox.meta.get('Type', '?')}, JMStudio {fox.meta.get('SoftVersion', '?')})")
    print("  native scanning uses this automatically -- no checkerboard needed.")
    return 0


def cmd_capture(args) -> int:
    from .capture.fox import capture_pairs
    from .project import Project
    proj = Project.find(args.target)
    manifest = capture_pairs(
        proj.path / "captures",
        pairs=args.pairs,
        interval=args.interval,
        preview=args.preview,
    )
    good = [p for p in manifest["pairs"] if "error" not in p]
    print(f"Captured {len(good)} stereo pair(s) -> {manifest['dir']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="foxstudio",
        description="Offline reverse-engineering workflow for 3DMakerpro Fox scans.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="create a scan project (auto-named customer_part_date)")
    p.add_argument("part")
    p.add_argument("--customer", "-c")
    p.add_argument("--vehicle", "-v", help="vehicle/machine the part belongs to")
    p.add_argument("--notes")
    p.add_argument("--scan", action="append", help="scan file to import (repeatable)")
    p.add_argument("--photo", action="append", help="reference photo to attach (repeatable)")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("import", help="import scans (STL/OBJ/PLY/ASC; RSCAN kept as placeholder)")
    p.add_argument("files", nargs="+")
    p.add_argument("--project", "-p", help="existing project (default: create new)")
    p.add_argument("--part", help="part name for a new project (default: first filename)")
    p.add_argument("--customer", "-c")
    p.add_argument("--vehicle", "-v", help="vehicle/machine for a new project")
    p.add_argument("--photo", action="append", help="reference photo to attach (repeatable)")
    p.add_argument("--measure", action="store_true", help="measure after import")
    p.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("list", help="list local projects")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("measure", help="bounding box, volume, surface area")
    p.add_argument("target", help="project name or geometry file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("clean", help="cleanup helpers (outliers, islands, holes, smoothing)")
    p.add_argument("target", help="project name or geometry file")
    p.add_argument("--voxel", type=float, help="point clouds: downsample voxel size in mm")
    p.add_argument("--stat-neighbors", type=int, help="point clouds: statistical outlier neighbors (default 20)")
    p.add_argument("--stat-std", type=float, help="point clouds: outlier std ratio (default 2.0)")
    p.add_argument("--radius", type=float, help="point clouds: radius outlier removal radius in mm")
    p.add_argument("--smooth", type=int, help="meshes: Taubin smoothing iterations")
    p.add_argument("--decimate", type=int, help="meshes: target triangle count")
    p.add_argument("--min-island", type=float, help="meshes: drop islands under this fraction of the largest (default 0.01)")
    p.add_argument("--no-fill-holes", action="store_true", help="meshes: skip hole filling")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("mesh", help="register + fuse project point clouds, Poisson-mesh them")
    p.add_argument("target", help="project name")
    p.add_argument("--voxel", type=float, default=1.0, help="registration voxel size in mm (default 1.0)")
    p.add_argument("--depth", type=int, default=9, help="Poisson octree depth (default 9)")
    p.set_defaults(func=cmd_mesh)

    p = sub.add_parser("export", help="export STL/OBJ for printing or CAD")
    p.add_argument("target", help="project name or geometry file")
    p.add_argument("--format", "-f", default="stl", choices=["stl", "obj", "ply"])
    p.add_argument("--scale", type=float, default=1.0, help="uniform scale factor (e.g. 0.001 mm->m)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("package", help="build a printable project folder (mesh + report + preview + photos)")
    p.add_argument("target", help="project name")
    p.add_argument("--format", "-f", default="stl", choices=["stl", "obj", "ply"], help=argparse.SUPPRESS)
    p.add_argument("--scale", type=float, default=1.0, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("publish", help="push a completed part to the WordPress parts library "
                                       "(3dpb-parts-library plugin)")
    p.add_argument("target", help="project name")
    p.add_argument("--category", help="part_category term (e.g. Automotive)")
    p.add_argument("--type", help="part_type term (e.g. Bracket)")
    p.add_argument("--material", help="part_material term (e.g. PETG)")
    p.add_argument("--brand", help="part_brand term (e.g. Toyota)")
    p.add_argument("--dry-run", action="store_true", help="print the payload without pushing")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("sync", help="retry queued WordPress publishes")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("gui", help="launch FoxStudio Desktop")
    p.set_defaults(func=lambda a: __import__("foxstudio.gui.app", fromlist=["main"]).main())

    p = sub.add_parser("probe", help="report the Fox's USB interfaces on this machine")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("calib", help="import/show the Fox factory calibration from JMStudio")
    p.add_argument("--serial", help="unit serial to match (default: first found)")
    p.set_defaults(func=cmd_calib)

    p = sub.add_parser("turntable", help="test/jog the Ciclop turntable + lasers")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--rotate", type=float, help="rotate this many degrees")
    p.add_argument("--laser-test", action="store_true", help="blink each laser")
    p.set_defaults(func=cmd_turntable)

    p = sub.add_parser("turntable-calibrate", help="calibrate the turntable axis (checkerboard on platform)")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--views", type=int, default=12, help="checkerboard views around 360 deg")
    p.set_defaults(func=cmd_turntable_calibrate)

    p = sub.add_parser("laser-aim", help="live preview to aim the lasers (line overlay in both cameras)")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--laser", type=int, default=0, choices=[0, 1], help="which laser to light while aiming")
    p.add_argument("--threshold", type=int, default=60, help="laser brightness threshold (0-255)")
    p.add_argument("--exposure", type=int, default=-11, help="camera exposure (log2 s; lower = darker)")
    p.set_defaults(func=cmd_laser_aim)

    p = sub.add_parser("laser-scan", help="turntable laser-line stereo scan into a project")
    p.add_argument("target", help="project name")
    p.add_argument("--port", help="serial port (default: auto-detect)")
    p.add_argument("--steps", type=int, default=200, help="turntable steps around 360 (default 200)")
    p.add_argument("--threshold", type=int, default=40, help="laser brightness threshold (0-255)")
    p.set_defaults(func=cmd_laser_scan)

    p = sub.add_parser("xu", help="read-only map (or live watch) of the Fox's UVC extension-unit controls")
    p.add_argument("--camera", default="JMM", help="camera name substring (default: JMM)")
    p.add_argument("--watch", action="store_true", help="poll controls and print changes while JMStudio scans")
    p.add_argument("--seconds", type=float, default=40.0, help="watch duration (default 40s)")
    p.set_defaults(func=cmd_xu)

    p = sub.add_parser("capture", help="direct stereo-pair capture from the Fox cameras")
    p.add_argument("target", help="project name")
    p.add_argument("--pairs", type=int, default=1)
    p.add_argument("--interval", type=float, default=0.5, help="seconds between pairs")
    p.add_argument("--preview", action="store_true", help="show a live A|B window")
    p.set_defaults(func=cmd_capture)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, IOError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
