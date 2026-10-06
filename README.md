# FoxStudio

Offline reverse-engineering workflow for the **3DMakerpro Fox** on Windows and
Linux. Everything runs locally; the only network feature is the optional
WordPress push, which queues automatically when offline.

## FoxStudio Desktop

Launch with `foxstudio-desktop` (or `foxstudio gui`). The desktop app covers
the whole workflow without touching a terminal:

- **Projects** panel: create jobs (customer, part, vehicle, notes), import
  JMStudio exports and photos.
- **3D Model** tab: orbit/zoom viewer for the current geometry.
- **Scan with the Fox** tab: live dual-camera preview and **native scanning
  without JMStudio** — one-time checkerboard stereo calibration, then each
  Scan press reconstructs a colored point cloud straight into the project.
  Rotate the part between scans and press *Fuse & Mesh* to merge them.
  Passive stereo needs surface texture; for smooth featureless parts, scan in
  JMStudio and import the export instead (see docs/FOX_USB_FINDINGS.md for
  the projector-protocol roadmap that would remove this limitation).
- One-click Measure / Clean / Fuse & Mesh / Export / Package / Publish.

Close JMStudio before connecting — it holds the Fox's cameras exclusively.
If the Fox doesn't appear, press its power button (it sleeps when idle).

## CLI

```
JMStudio scan ──export──> STL/OBJ/PLY/ASC ──> foxstudio import ──> measure/clean/mesh ──> export STL/OBJ ──> print / CAD
                                                                                    └──> publish (WordPress, queued offline)
Fox USB (UVC) ─────────────────────────────> foxstudio capture (direct stereo pairs)
```

## Install

```
pip install -e .            # from this directory
# Windows extra (camera name lookup):
pip install pygrabber
```

Requires Python 3.9–3.11 (Open3D constraint). Dependencies: numpy, open3d,
opencv-python, trimesh, requests.

## Quick start

```bash
# full project intake: customer, vehicle/machine, notes, photos, scans
foxstudio new "water pump housing" -c "Acme Machine" -v "1987 Case 1845C" \
    --notes "cracked flange" --scan left.asc --scan right.asc --photo part.jpg

# or import-first; auto-creates a project named customer_part_date
foxstudio import bracket.stl --part "mounting bracket" --customer "acme" --measure

foxstudio list
foxstudio measure acme-mounting          # bbox / volume / area / watertight / scale check
foxstudio clean acme-mounting            # cleans every scan: outliers, islands, holes, normals
foxstudio mesh acme-mounting             # ICP-align + fuse all scans -> Poisson mesh
foxstudio export acme-mounting -f stl    # export/<slug>.stl + report.json + preview.png
foxstudio package acme-mounting          # printable folder: mesh, report, preview, photos, notes
foxstudio publish acme-mounting          # WordPress create-or-update (queues if offline)
foxstudio sync                           # flush the offline queue later
```

Projects live in `~/FoxStudio/projects/<customer>_<part>_<YYYY-MM-DD>/` with
`raw/`, `cleaned/`, `export/`, `captures/` and a `project.json` metadata file.
Any command that takes a project name also accepts a unique substring, or a
bare geometry file path for one-off use.

Projects live in `~/FoxStudio/projects/` (configurable via
`~/.foxstudio/config.json`), one folder per part with `raw/`, `cleaned/`,
`export/`, `captures/`, `photos/` and `project.json`.

## Accepted formats

- **Import:** STL, OBJ, PLY (mesh or point cloud), ASC/XYZ/PTS point clouds
  (JMStudio's plain-text `x y z [nx ny nz] [r g b]` export). JMStudio
  `.rscan` project files are stored with the job as **placeholders** — they
  are proprietary; export STL/OBJ/PLY/ASC from JMStudio to process geometry.
  Photos (JPG/PNG/...) passed anywhere land in `photos/`.
- **Export:** STL, OBJ (meshes), PLY. `--scale` applies a uniform factor
  (JMStudio exports are millimetres).

## Mesh analysis

`foxstudio measure` reports axis-aligned and oriented (best-fit) bounding
boxes, surface area, triangle count, a watertight check, and volume — exact
when watertight, otherwise the convex-hull volume as a labeled upper-bound
estimate. A **scale sanity check** warns when the largest dimension is under
2 mm or over 2 m (wrong-units symptoms). Results are stored in
`project.json`, written to `export/report.json`, and included in the
WordPress post.

## Cleanup, alignment, and meshing (Open3D)

- `clean` processes **every** scan in the project. Point clouds: voxel
  downsample (`--voxel`), statistical and radius outlier removal, normal
  estimation. Meshes: duplicate/degenerate/non-manifold removal, isolated
  component removal (`--min-island`), hole filling, consistent normal
  reorientation, Taubin smoothing (`--smooth`), quadric decimation
  (`--decimate`).
- `mesh` aligns all scans (meshes are point-sampled automatically):
  FPFH + RANSAC global init, point-to-plane ICP refinement, multiway
  pose-graph optimization, fusion, then Poisson reconstruction with
  low-density trimming. Verified to sub-0.1 mm on overlapping synthetic
  partial scans.

## WordPress parts library (3dpb-parts-library plugin)

`publish` integrates natively with the **3dpb-parts-library** plugin: it
creates a `part_library_item` (or **updates** it on re-publish) and

- fills the plugin's meta fields — `equipment_type` (from `--vehicle`),
  `can_scan: yes`, `can_print_direct: yes`, `status` (config `part_status`,
  default `available`);
- assigns the plugin's taxonomies from `--category/--type/--material/--brand`
  flags or config defaults, creating terms when they don't exist yet;
- uploads `export/preview.png` as the featured image and the project's
  `photos/` into the plugin's gallery;
- attaches the cleaned meshes from `export/` with download links.

The customer name is **never** included in the public post — it stays in the
local `project.json`. Use `foxstudio publish <project> --dry-run` to inspect
the payload before pushing.

Create an Application Password in WordPress (Users → Profile), then edit
`~/.foxstudio/config.json`:

```json
{
  "wordpress": {
    "url": "https://yoursite.com",
    "user": "derek",
    "app_password": "xxxx xxxx xxxx xxxx",
    "post_type": "part_library_item",
    "status": "draft",
    "part_status": "available",
    "part_category": "Automotive",
    "part_material": "",
    "verify_tls": true
  }
}
```

Set `verify_tls` to `false` for dev sites with self-signed certificates
(e.g. Local's `*.local` domains). Setting `post_type` to anything else falls
back to a generic post (no plugin meta/taxonomies).

WordPress blocks STL/OBJ uploads by default; allow them with a small
mu-plugin (already installed on the local 3dprintingballarat site as
`wp-content/mu-plugins/3dpb-allow-model-uploads.php` — copy it to production
when the library goes live):

```php
<?php // wp-content/mu-plugins/3dpb-allow-model-uploads.php
add_filter('upload_mimes', function ($m) {
    $m['stl'] = 'model/stl';
    $m['obj'] = 'model/obj';
    $m['ply'] = 'application/octet-stream';
    return $m;
});
// WP's finfo check can't identify model formats; trust the extension for these three.
add_filter('wp_check_filetype_and_ext', function ($data, $file, $filename) {
    $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
    $map = ['stl' => 'model/stl', 'obj' => 'model/obj', 'ply' => 'application/octet-stream'];
    if (isset($map[$ext]) && empty($data['type'])) {
        $data = ['ext' => $ext, 'type' => $map[$ext], 'proper_filename' => false];
    }
    return $data;
}, 10, 3);
```

If the site is unreachable, the payload is queued in `~/.foxstudio/queue/`
and `foxstudio sync` pushes it when you're back online.

## Direct capture from the Fox

The Fox exposes its two cameras as **standard UVC devices** (verified —
see [docs/FOX_USB_FINDINGS.md](docs/FOX_USB_FINDINGS.md)):

```bash
foxstudio probe                       # what the Fox exposes on this machine
foxstudio capture <project> --pairs 12 --preview
```

`capture` saves synchronized A/B stereo frame pairs to the project's
`captures/` folder. **Close JMStudio first** — it holds the camera streams
exclusively. There is no depth stream on the wire; see the findings doc for
the path from stereo pairs to independent depth reconstruction.
