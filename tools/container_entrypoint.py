"""Linux container launchers; never rebuild rasters or download data at startup."""
import importlib.resources
import json
import os
from pathlib import Path
import shutil
import sys

from bathymetry import default_directory, package_path

TOOLS = Path(__file__).resolve().parent


def prepare_projection():
    """Pair the portable NOAA grid with this image's matching PROJ databases.

    The installed data may have been built on Windows or by a different wheel
    version. Copy only the small runtime files, leaving all source data intact.
    """
    root = default_directory()
    active = root / "manifest.json"
    if not active.is_file():
        return  # Allow infrastructure-only use and initial installation workers.
    manifest = json.loads(active.read_text(encoding="utf-8"))
    datum = manifest.get("datumGrid")
    if not datum:
        return
    grid = package_path(root, datum["file"])
    runtime = Path(os.environ["BLUEWATER_PROJ_DIR"])
    sources = {
        "pyproj": Path(str(importlib.resources.files("pyproj") / "proj_dir" / "share" / "proj")),
        "rasterio": Path(str(importlib.resources.files("rasterio") / "proj_data")),
    }
    for name, source in sources.items():
        target = runtime / name
        target.mkdir(parents=True, exist_ok=True)
        for filename in ("proj.db", "proj.ini"):
            if (source / filename).is_file():
                shutil.copy2(source / filename, target / filename)
        if not (target / "proj.db").is_file():
            raise RuntimeError(f"Missing {name} PROJ database in the container image.")
        shutil.copy2(grid, target / grid.name)


def main():
    os.environ.setdefault("BLUEWATER_PROJ_DIR", "/tmp/bluewater-proj")
    if os.environ.get("BLUEWATER_STORAGE_BACKEND", "local") == "local":
        prepare_projection()
    mode, *args = sys.argv[1:]
    if mode == "web":
        if args:
            raise SystemExit("Web settings are provided through environment variables.")
        command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
                   "--port", os.environ.get("PORT", "8080"), "--workers", "1",
                   "--timeout-keep-alive", "5", "--no-server-header"]
    elif mode == "worker":
        if args and args[0] == "cloud":
            credentials = os.environ.pop("BLUEWATER_COPERNICUS_CREDENTIALS", None)
            if credentials:
                try:
                    values = json.loads(credentials)
                    for field in ("username", "password"):
                        if not isinstance(values.get(field), str) or not values[field]:
                            raise ValueError()
                    os.environ["BLUEWATER_COPERNICUS_USERNAME"] = values["username"]
                    os.environ["BLUEWATER_COPERNICUS_PASSWORD"] = values["password"]
                except (ValueError, TypeError):
                    raise SystemExit("The Copernicus secret must contain a username and password.")
            command = [sys.executable, "-u", str(TOOLS / "publish_cloud.py"), *args[1:]]
            os.execv(sys.executable, command)
        jobs = {"snapshots": "build_snapshots.py", "production": "build_production.py",
                "currents": "build_currents.py", "bathymetry": "build_bathymetry.py",
                "contours": "build_contours.py"}
        if not args or args[0] not in jobs:
            raise SystemExit("Choose a worker: " + ", ".join(jobs))
        if os.environ.get("BLUEWATER_STORAGE_BACKEND", "local") == "gcs":
            raise SystemExit("In GCS mode use: worker cloud build --kind infra|production|currents|contours -- [builder arguments].")
        command = [sys.executable, "-u", str(TOOLS / jobs[args[0]]), *args[1:]]
    else:
        raise SystemExit("Choose web or worker.")
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
