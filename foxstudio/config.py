"""Configuration and paths. Works on Windows and Linux, fully offline.

Config lives in ~/.foxstudio/config.json; scan projects default to ~/FoxStudio/projects.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

CONFIG_DIR = Path.home() / ".foxstudio"
CONFIG_FILE = CONFIG_DIR / "config.json"
QUEUE_DIR = CONFIG_DIR / "queue"

DEFAULTS = {
    "projects_dir": str(Path.home() / "FoxStudio" / "projects"),
    "default_customer": "shop",
    "units": "mm",
    # WordPress push (optional; leave url empty to disable)
    "wordpress": {
        "url": "",              # e.g. https://3dprintingballarat.com.au
        "user": "",             # WP username
        "app_password": "",     # WP Application Password
        # "part_library_item" targets the 3dpb-parts-library plugin natively
        # (meta fields, taxonomies, gallery); any other value posts generically.
        "post_type": "part_library_item",
        "status": "draft",
        # 3dpb-parts-library defaults applied to new items
        "part_status": "available",   # available|possible|needs_sample|needs_cad|experimental|not_recommended
        "part_category": "",          # default term names; override per publish with CLI flags
        "part_type": "",
        "part_material": "",
        "part_brand": "",
    },
    # Fox direct capture
    "capture": {
        "camera_a": "JMM8004902_A",  # substring match on device name (Windows)
        "camera_b": "JMM8004902_B",
        "width": 1280,
        "height": 720,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _merge(base[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            # utf-8-sig: tolerate the BOM that Notepad/PowerShell prepend
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: ignoring invalid {CONFIG_FILE}: {exc}", file=sys.stderr)
            user = {}
        return _merge(DEFAULTS, user)
    return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def projects_dir() -> Path:
    p = Path(load_config()["projects_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


def queue_dir() -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR
