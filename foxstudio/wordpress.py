"""Push completed parts to the WordPress parts library, with an offline queue.

Integrates natively with the **3dpb-parts-library** plugin when post_type is
"part_library_item" (the default): populates its `_tdpb_parts_*` meta fields
(equipment_type, can_scan, status, ...), assigns its taxonomies
(part_category/part_type/part_material/part_brand, creating terms as needed),
sets the preview render as the featured image, and uploads photos into the
plugin's gallery. Any other post_type falls back to a plain post.

Auth is a WordPress Application Password (Users -> Profile). Configure in
~/.foxstudio/config.json under "wordpress".

The customer name is deliberately NOT included in the public post -- the parts
library is public; customer details stay in the local project.json.

When offline (or the site is unreachable) the publish payload is written to
~/.foxstudio/queue/ and `foxstudio sync` retries later. STL/OBJ attachments are
uploaded as media; WordPress blocks model mime types by default, so allow them
with a small mu-plugin (see README) or the queue entry notes the skip.
"""
from __future__ import annotations

import json
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from .config import load_config, queue_dir

TIMEOUT = 15

MIME_OVERRIDES = {
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".ply": "application/octet-stream",
    ".asc": "text/plain",
}


class WordPressError(RuntimeError):
    pass


def _settings() -> dict:
    wp = load_config()["wordpress"]
    if not wp.get("url") or not wp.get("user") or not wp.get("app_password"):
        raise WordPressError(
            "WordPress is not configured. Set wordpress.url/user/app_password "
            "in ~/.foxstudio/config.json")
    return wp


def _auth(wp: dict):
    return (wp["user"], wp["app_password"])


def _json(r: requests.Response):
    """Parse a JSON response tolerating a leading UTF-8 BOM (emitted by WP
    installs where a PHP file was saved with a BOM)."""
    return json.loads(r.content.decode("utf-8-sig"))


def _verify(wp: dict) -> bool:
    """Set "verify_tls": false in config for dev sites with self-signed certs
    (e.g. Local's *.local domains)."""
    verify = wp.get("verify_tls", True)
    if not verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return verify


def is_online(wp: dict) -> bool:
    try:
        requests.head(wp["url"], timeout=5, allow_redirects=True, verify=_verify(wp))
        return True
    except requests.RequestException:
        return False


def build_payload(project, terms: Optional[dict] = None) -> dict:
    """Assemble the post body, part-library meta/terms, and attachments.

    `terms` optionally maps taxonomy -> term name (part_category, part_type,
    part_material, part_brand), overriding the config defaults.
    """
    wp = load_config()["wordpress"]
    meta = project.meta
    m = meta.get("measurements", {})
    lines = []
    if meta.get("vehicle"):
        lines.append(f"Fits: {meta['vehicle']}")
    bb = m.get("bbox_mm")
    if bb:
        lines.append(f"Dimensions: {bb['x']} x {bb['y']} x {bb['z']} mm")
    if "volume_cm3" in m:
        lines.append(f"Volume: {m['volume_cm3']} cm3")
    elif "hull_volume_cm3" in m:
        lines.append(f"Volume (estimate): {m['hull_volume_cm3']} cm3")
    if "surface_area_mm2" in m:
        lines.append(f"Surface area: {m['surface_area_mm2']} mm2")
    lines.append("3D scanned from an original part; print-ready model available.")

    exports = sorted((project.path / "export").glob("*"))
    preview = [f for f in exports if f.stem == "preview" and f.suffix.lower() == ".png"]
    models = [f for f in exports if f.suffix.lower() in MIME_OVERRIDES]
    photos = sorted(str(f) for f in (project.path / "photos").glob("*")
                    if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    published = meta.get("published") or {}

    tax_terms = {}
    for taxonomy in ("part_category", "part_type", "part_material", "part_brand"):
        name = (terms or {}).get(taxonomy) or wp.get(taxonomy, "")
        if name:
            tax_terms[taxonomy] = name

    return {
        "title": meta.get("part", project.path.name),
        "content": "<br/>\n".join(lines),
        "slug": meta.get("slug"),
        "attachments": [str(f) for f in models],
        "preview": str(preview[0]) if preview else None,
        "photos": photos,
        "post_id": published.get("post_id"),  # set -> update instead of create
        # 3dpb-parts-library meta (used when post_type is part_library_item)
        "part_meta": {
            "_tdpb_parts_equipment_type": meta.get("vehicle", ""),
            "_tdpb_parts_can_scan": "yes",
            "_tdpb_parts_can_print_direct": "yes",
            "_tdpb_parts_status": wp.get("part_status", "available"),
        },
        "terms": tax_terms,
    }


def _resolve_term(wp: dict, taxonomy: str, name: str) -> Optional[int]:
    """Find a term id by name (case-insensitive), creating the term if missing."""
    base = wp["url"].rstrip("/")
    r = requests.get(f"{base}/wp-json/wp/v2/{taxonomy}",
                     params={"search": name, "per_page": 50},
                     auth=_auth(wp), timeout=TIMEOUT, verify=_verify(wp))
    r.raise_for_status()
    for term in _json(r):
        if term.get("name", "").lower() == name.lower():
            return int(term["id"])
    r = requests.post(f"{base}/wp-json/wp/v2/{taxonomy}",
                      json={"name": name}, auth=_auth(wp), timeout=TIMEOUT,
                      verify=_verify(wp))
    if r.status_code == 400 and _json(r).get("code") == "term_exists":
        return int(_json(r)["data"]["term_id"])
    r.raise_for_status()
    return int(_json(r)["id"])


def _upload_media(wp: dict, path: Path) -> dict:
    mime = MIME_OVERRIDES.get(path.suffix.lower()) or \
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        r = requests.post(
            f"{wp['url'].rstrip('/')}/wp-json/wp/v2/media",
            auth=_auth(wp),
            headers={
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "Content-Type": mime,
            },
            data=fh,
            timeout=60,
            verify=_verify(wp),
        )
    # WP signals a blocked file type as 415, 400, or a 500 with code
    # rest_upload_sideload_error ("not allowed to upload this file type").
    blocked = (r.status_code == 415
               or (r.status_code == 400 and "mime" in r.text.lower())
               or (r.status_code == 500 and "rest_upload_sideload_error" in r.text))
    if blocked:
        return {"skipped": path.name,
                "reason": "file type blocked by WordPress (allow model/stl via a mu-plugin)"}
    r.raise_for_status()
    j = _json(r)
    return {"id": j["id"], "url": j.get("source_url"), "file": path.name}


def push(payload: dict) -> dict:
    """Send one payload (post + preview + attachments) to WordPress.
    Creates the part-library item, or updates it when payload['post_id'] is set."""
    wp = _settings()
    base = wp["url"].rstrip("/")

    is_parts_library = wp.get("post_type", "posts") == "part_library_item"

    featured_id = None
    media_results = []
    if payload.get("preview") and Path(payload["preview"]).exists():
        res = _upload_media(wp, Path(payload["preview"]))
        media_results.append(res)
        featured_id = res.get("id")

    gallery_ids = []
    for photo in payload.get("photos", []):
        p = Path(photo)
        if not p.exists():
            continue
        res = _upload_media(wp, p)
        media_results.append(res)
        if res.get("id"):
            gallery_ids.append(res["id"])
        if featured_id is None and res.get("id"):
            featured_id = res["id"]  # no preview render? lead with a photo

    links = []
    for att in payload.get("attachments", []):
        p = Path(att)
        if not p.exists():
            media_results.append({"skipped": att, "reason": "file missing"})
            continue
        res = _upload_media(wp, p)
        media_results.append(res)
        if res.get("url"):
            links.append(f'<a href="{res["url"]}">{res["file"]}</a>')

    content = payload["content"]
    if links:
        content += "<br/>\nDownloads: " + " | ".join(links)

    body = {
        "title": payload["title"],
        "content": content,
        "status": wp.get("status", "draft"),
        "slug": payload.get("slug"),
    }
    if featured_id:
        body["featured_media"] = featured_id

    if is_parts_library:
        meta = {k: v for k, v in payload.get("part_meta", {}).items() if v}
        if gallery_ids:
            meta["_tdpb_parts_gallery"] = gallery_ids
        if meta:
            body["meta"] = meta
        for taxonomy, name in payload.get("terms", {}).items():
            term_id = _resolve_term(wp, taxonomy, name)
            if term_id:
                body[taxonomy] = [term_id]

    route = f"{base}/wp-json/wp/v2/{wp.get('post_type', 'posts')}"
    if payload.get("post_id"):
        route += f"/{payload['post_id']}"
    r = requests.post(route, auth=_auth(wp), json=body, timeout=TIMEOUT, verify=_verify(wp))
    if r.status_code == 404 and payload.get("post_id"):
        # post was deleted on the site; create a fresh one
        r = requests.post(route.rsplit("/", 1)[0], auth=_auth(wp), json=body,
                          timeout=TIMEOUT, verify=_verify(wp))
    r.raise_for_status()
    j = _json(r)
    return {
        "post_id": j["id"],
        "link": j.get("link"),
        "status": j.get("status"),
        "media": media_results,
        "pushed_at": datetime.now().isoformat(timespec="seconds"),
    }


def enqueue(payload: dict) -> Path:
    entry = queue_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.json"
    entry.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return entry


def publish_or_queue(project, terms: Optional[dict] = None) -> dict:
    """Publish now if the site is reachable; otherwise queue for `foxstudio sync`."""
    payload = build_payload(project, terms)
    payload["project_path"] = str(project.path)
    wp = _settings()
    if not is_online(wp):
        entry = enqueue(payload)
        return {"queued": str(entry)}
    result = push(payload)
    project.meta["published"] = result
    project.save()
    return result


def sync_queue() -> list[dict]:
    """Retry every queued publish. Returns a result per entry."""
    results = []
    for entry in sorted(queue_dir().glob("*.json")):
        payload = json.loads(entry.read_text(encoding="utf-8"))
        try:
            result = push(payload)
            entry.unlink()
            results.append({"entry": entry.name, "ok": True, **result})
            proj_path = payload.get("project_path")
            if proj_path and Path(proj_path, "project.json").exists():
                from .project import Project
                proj = Project(Path(proj_path))
                proj.meta["published"] = result
                proj.save()
        except (requests.RequestException, WordPressError) as exc:
            results.append({"entry": entry.name, "ok": False, "error": str(exc)})
    return results
