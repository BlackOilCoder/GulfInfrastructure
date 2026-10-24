"""Prepare matching runtime PROJ files without modifying an installed package."""
import importlib.resources
from pathlib import Path
import shutil


def prepare_projection_files(grid, target_root):
    sources = {
        "pyproj": Path(str(importlib.resources.files("pyproj") / "proj_dir" / "share" / "proj")),
        "rasterio": Path(str(importlib.resources.files("rasterio") / "proj_data")),
    }
    for name, source in sources.items():
        target = Path(target_root) / name
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("proj.db", "proj.ini"):
            if (source / filename).is_file():
                shutil.copy2(source / filename, target / filename)
        if not (target / "proj.db").is_file():
            raise RuntimeError(f"Missing {name} PROJ database in the runtime.")
        shutil.copy2(grid, target / grid.name)
