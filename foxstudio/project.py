"""Local scan-project store with auto-naming by part/customer/date.

Layout of a project directory:
    <projects_dir>/<slug>/
        project.json     metadata (part, customer, dates, measurements, publish state)
        raw/             imported JMStudio exports (STL/OBJ/PLY/ASC), untouched
        cleaned/         outputs of cleanup / meshing steps
        export/          final STL/OBJ for printing or CAD
        captures/        direct-capture frames and point clouds
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from .config import load_config, projects_dir

SUBDIRS = ("raw", "cleaned", "export", "captures", "photos")

# Formats we can parse vs. formats stored as placeholders (e.g. JMStudio .rscan
# project files: keep them with the job, export STL/OBJ/PLY/ASC to process).
PLACEHOLDER_EXTS = {".rscan", ".jmproj"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic"}


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "untitled"


def make_slug(part: str, customer: str, when: Optional[date] = None) -> str:
    when = when or date.today()
    base = f"{slugify(customer)}_{slugify(part)}_{when.isoformat()}"
    slug = base
    n = 2
    while (projects_dir() / slug).exists():
        slug = f"{base}-{n:02d}"
        n += 1
    return slug


class Project:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.meta_file = self.path / "project.json"
        self.meta: dict = {}
        if self.meta_file.exists():
            self.meta = json.loads(self.meta_file.read_text(encoding="utf-8"))

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def create(cls, part: str, customer: Optional[str] = None, notes: str = "",
               vehicle: str = "") -> "Project":
        cfg = load_config()
        customer = customer or cfg["default_customer"]
        slug = make_slug(part, customer)
        path = projects_dir() / slug
        for sub in SUBDIRS:
            (path / sub).mkdir(parents=True, exist_ok=True)
        proj = cls(path)
        proj.meta = {
            "slug": slug,
            "part": part,
            "customer": customer,
            "vehicle": vehicle,
            "notes": notes,
            "units": cfg["units"],
            "created": datetime.now().isoformat(timespec="seconds"),
            "files": [],
            "measurements": {},
            "published": None,
        }
        proj.save()
        return proj

    @classmethod
    def find(cls, name: str) -> "Project":
        """Resolve a project by exact slug, unique substring, or path."""
        p = Path(name)
        if p.is_dir() and (p / "project.json").exists():
            return cls(p)
        root = projects_dir()
        exact = root / name
        if (exact / "project.json").exists():
            return cls(exact)
        matches = [d for d in root.iterdir()
                   if d.is_dir() and name.lower() in d.name.lower()
                   and (d / "project.json").exists()]
        if len(matches) == 1:
            return cls(matches[0])
        if not matches:
            raise FileNotFoundError(f"No project matching '{name}' in {root}")
        raise ValueError(f"'{name}' is ambiguous: {', '.join(m.name for m in matches)}")

    @staticmethod
    def list_all() -> list["Project"]:
        root = projects_dir()
        return [Project(d) for d in sorted(root.iterdir())
                if d.is_dir() and (d / "project.json").exists()]

    def save(self) -> None:
        self.meta["updated"] = datetime.now().isoformat(timespec="seconds")
        self.meta_file.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    # -- files -------------------------------------------------------------
    def _copy_unique(self, src: Path, subdir: str) -> Path:
        dest = self.path / subdir / src.name
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            n = 2
            while dest.exists():
                dest = dest.with_name(f"{stem}-{n:02d}{suffix}")
                n += 1
        shutil.copy2(src, dest)
        return dest

    def import_file(self, src: Path) -> tuple[Path, str]:
        """Copy a scan file into raw/ and record it.

        Returns (dest, role) where role is 'raw' for parseable geometry and
        'placeholder' for formats we keep but cannot process (e.g. .rscan).
        """
        src = Path(src)
        role = "placeholder" if src.suffix.lower() in PLACEHOLDER_EXTS else "raw"
        dest = self._copy_unique(src, "raw")
        self.meta["files"].append({
            "name": dest.name,
            "role": role,
            "imported": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
        })
        self.save()
        return dest, role

    def import_photo(self, src: Path) -> Path:
        dest = self._copy_unique(Path(src), "photos")
        self.meta["files"].append({
            "name": dest.name,
            "role": "photo",
            "imported": datetime.now().isoformat(timespec="seconds"),
            "source": str(src),
        })
        self.save()
        return dest

    def geometry_files(self, prefer_cleaned: bool = True) -> list[Path]:
        """All parseable geometry files: cleaned/ if present, else raw/."""
        subs = ("cleaned", "raw") if prefer_cleaned else ("raw",)
        for sub in subs:
            files = sorted(f for f in (self.path / sub).glob("*")
                           if f.suffix.lower() in (".stl", ".obj", ".ply", ".asc", ".xyz", ".pts"))
            if files:
                return files
        return []

    def latest_geometry(self) -> Path:
        """Best current geometry: newest file in cleaned/, else newest in raw/."""
        files = self.geometry_files()
        if not files:
            raise FileNotFoundError(f"Project '{self.meta.get('slug')}' has no geometry files yet")
        return max(files, key=lambda f: f.stat().st_mtime)
