"""Local BOEM raster access. Display tiles and native-resolution depth samples are independent."""
from collections import OrderedDict
from contextlib import closing
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import threading

WORLD = 20037508.342789244
TILE_SIZE = 256
MAX_POINTS = 5000
_projection_lock = threading.Lock()


def configure_projection(directory):
    """Use installed datum grids without making network requests at runtime."""
    import pyproj
    with _projection_lock:
        grids = Path(os.environ.get("BLUEWATER_PROJ_DIR") or Path(directory) / "proj")
        if not all((grids / name / "proj.db").is_file() for name in ("pyproj", "rasterio")):
            # During a first worker installation the container startup has no
            # manifest yet. The builder installs matching databases in this root.
            grids = Path(directory) / "proj"
        python_data, raster_data = grids / "pyproj", grids / "rasterio"
        configured = (python_data / "proj.db").is_file() and (raster_data / "proj.db").is_file()
        if configured:
            pyproj.datadir.set_data_dir(str(python_data))
            os.environ["PROJ_DATA"] = str(raster_data)
        pyproj.network.set_network_enabled(False)
        os.environ["PROJ_NETWORK"] = "OFF"
        os.environ.setdefault("GDAL_CACHEMAX", "128")
        return str(raster_data) if configured else None


def default_directory():
    override = os.environ.get("BLUEWATER_BATHYMETRY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    config = Path(__file__).resolve().parent.parent / ".bluewater-local.json"
    if config.exists():
        try:
            location = json.loads(config.read_text(encoding="utf-8")).get("bathymetryDirectory")
            if location:
                return Path(location).expanduser().resolve()
        except (OSError, ValueError, TypeError):
            pass
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return base / "Bluewater" / "bathymetry"


def save_local_configuration(directory):
    # An explicit deployment path is already persistent configuration. Do not
    # write a machine-specific path into an immutable application image.
    if os.environ.get("BLUEWATER_BATHYMETRY_DIR"):
        return
    config = Path(__file__).resolve().parent.parent / ".bluewater-local.json"
    temporary = config.with_suffix(".tmp")
    temporary.write_text(json.dumps({"bathymetryDirectory": str(Path(directory).resolve())}, indent=2), encoding="utf-8")
    os.replace(temporary, config)


def package_path(directory, name):
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Raster package paths must remain within the bathymetry directory.")
    return path


def initialize_tiles(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=30)) as db:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
          CREATE TABLE IF NOT EXISTS tiles (
            zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
            PRIMARY KEY (zoom_level, tile_column, tile_row));
        """)
        db.commit()


class Bathymetry:
    def __init__(self, directory=None, manifest_data=None):
        self.directory = Path(directory or default_directory()).resolve()
        self._manifest = None
        self._override = manifest_data
        self._stamp = None
        self._lock = threading.RLock()
        self._tile_lock = threading.Lock()
        self._thread = threading.local()
        self._blocks = OrderedDict()
        self._tile_memory = OrderedDict()

    def manifest(self):
        if self._override is not None:
            return self._override
        path = self.directory / "manifest.json"
        with self._lock:
            stamp = (path.stat().st_mtime_ns, path.stat().st_size)
            if stamp != self._stamp:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("schemaVersion") != 1 or not data.get("sources"):
                    raise ValueError("The local bathymetry manifest is invalid.")
                for source in data["sources"]:
                    for field in ("depth", "hillshade"):
                        if not package_path(self.directory, source[field]).is_file():
                            raise FileNotFoundError(f"Missing local {field} raster for {source['id']}.")
                if data.get("datumGrid"):
                    for name in (data["datumGrid"]["file"], "proj/pyproj/proj.db", "proj/rasterio/proj.db"):
                        if not package_path(self.directory, name).is_file():
                            raise FileNotFoundError("The local coordinate correction files are missing. Re-run tools/install_bathymetry.bat.")
                package_path(self.directory, data["tiles"]["file"])
                self._manifest, self._stamp = data, stamp
                self._blocks.clear()
                self._tile_memory.clear()
            return self._manifest

    def status(self):
        try:
            data = self.manifest()
            missing = [name for name in ("rasterio", "pyproj", "numpy", "PIL") if importlib.util.find_spec(name) is None]
            if missing:
                raise RuntimeError("Start Bluewater with tools/start_bluewater.bat; raster dependencies are missing: " + ", ".join(missing))
            bounds = [s["boundsWgs84"] for s in data["sources"]]
            return {"ready": True, "version": data["version"], "source": "BOEM local bathymetry",
                    "generated": data["generated"], "units": "feet", "depthConvention": "positive-down",
                    "resolutionFt": data.get("resolutionFt", 40), "sampling": ["nearest", "bilinear"],
                    "bounds": [min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds)],
                    "tiles": {"maxZoom": data["tiles"]["maxZoom"], "tileSize": TILE_SIZE},
                    "sources": [{"id": s["id"], "bounds": s["boundsWgs84"], "crs": s["crs"]} for s in data["sources"]]}
        except (FileNotFoundError, OSError, ValueError, KeyError, RuntimeError) as exc:
            return {"ready": False, "message": "Local bathymetry is not installed. Run tools/install_bathymetry.bat." if not (self.directory / "manifest.json").exists() else str(exc)}

    def _handles(self, data=None):
        data = data or self.manifest()
        configure_projection(self._projection_directory(data))
        import rasterio
        import pyproj
        if getattr(self._thread, "version", None) != data["version"]:
            for entry in getattr(self._thread, "sources", []):
                entry["depth"].close()
                entry["shade"].close()
            handles = []
            for source in data["sources"]:
                depth = rasterio.open(self._raster_path(source["depth"]))
                shade = rasterio.open(self._raster_path(source["hillshade"]))
                transformer = pyproj.Transformer.from_crs("EPSG:4326", depth.crs, always_xy=True, allow_ballpark=False)
                handles.append({"meta": source, "depth": depth, "shade": shade, "transformer": transformer})
            self._thread.sources, self._thread.version = handles, data["version"]
        return self._thread.sources

    def _projection_directory(self, data):
        return self.directory

    def _raster_path(self, name):
        return package_path(self.directory, name)

    def _cached_tile(self, data, zoom, col, row):
        path = package_path(self.directory, data["tiles"]["file"])
        if path.exists():
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)) as db:
                found = db.execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                                   (zoom, col, 2**zoom-1-row)).fetchone()
            if found:
                return bytes(found[0])

    def _store_tile(self, data, zoom, col, row, payload):
        path = package_path(self.directory, data["tiles"]["file"])
        try:
            with self._tile_lock:
                initialize_tiles(path)
                with closing(sqlite3.connect(path, timeout=30)) as db:
                    db.execute("INSERT OR IGNORE INTO tiles VALUES (?,?,?,?)", (zoom, col, 2**zoom-1-row, payload))
                    db.commit()
        except (OSError, sqlite3.Error):
            pass

    def _block(self, source, row, col):
        import numpy as np
        from rasterio.windows import Window
        ds = source["depth"]
        bh, bw = ds.block_shapes[0]
        br, bc = row // bh, col // bw
        key = (self._thread.version, source["meta"]["id"], br, bc)
        with self._lock:
            cached = self._blocks.get(key)
            if cached is not None:
                self._blocks.move_to_end(key)
                return cached, row % bh, col % bw
        arr = ds.read(1, window=Window(bc * bw, br * bh, min(bw, ds.width-bc*bw), min(bh, ds.height-br*bh)), masked=True).astype("float64")
        arr = np.where(np.ma.getmaskarray(arr), np.nan, np.asarray(arr))
        with self._lock:
            self._blocks[key] = arr
            if len(self._blocks) > 64:
                self._blocks.popitem(last=False)
        return arr, row % bh, col % bw

    def _pixel(self, source, row, col):
        ds = source["depth"]
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            return None
        block, r, c = self._block(source, row, col)
        value = float(block[r, c])
        if not math.isfinite(value):
            return None
        # Preserve source scales/offsets and explicitly interpret signed elevations.
        value = value * ds.scales[0] + ds.offsets[0]
        factor = 1.0 if source["meta"]["units"] == "feet" else 1 / 0.3048
        depth = -value * factor if source["meta"].get("valueConvention", "elevation") == "elevation" else value * factor
        return depth if depth >= 0 and math.isfinite(depth) else None

    def sample(self, points, method="nearest"):
        if method not in ("nearest", "bilinear"):
            raise ValueError("Sampling must be nearest or bilinear.")
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
            raise ValueError(f"Send between 1 and {MAX_POINTS} longitude/latitude points.")
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in point) or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90:
                raise ValueError("Each point must contain finite WGS 84 longitude and latitude.")
        data = self.manifest()
        handles = self._handles(data)
        result = [{"depthFt": None, "status": "outside-coverage"} for _ in points]
        for source in handles:
            ds = source["depth"]
            xs, ys = source["transformer"].transform([p[0] for p in points], [p[1] for p in points])
            requests = []
            for i, (x, y) in enumerate(zip(xs, ys)):
                if result[i]["depthFt"] is not None or not (math.isfinite(x) and math.isfinite(y)):
                    continue
                col, row = (~ds.transform) * (x, y)
                if 0 <= row < ds.height and 0 <= col < ds.width:
                    requests.append((int(row) // ds.block_shapes[0][0], int(col) // ds.block_shapes[0][1], i, row, col))
            for _, _, i, row, col in sorted(requests):
                value = None
                if method == "nearest":
                    value = self._pixel(source, math.floor(row), math.floor(col))
                else:
                    # Coordinates relative to pixel centers; never interpolate across NoData.
                    r, c = row - .5, col - .5
                    r0, c0 = math.floor(r), math.floor(c)
                    values = [self._pixel(source, r0+dy, c0+dx) for dy in (0, 1) for dx in (0, 1)]
                    if all(v is not None for v in values):
                        tx, ty = c-c0, r-r0
                        value = values[0]*(1-tx)*(1-ty)+values[1]*tx*(1-ty)+values[2]*(1-tx)*ty+values[3]*tx*ty
                result[i] = {"depthFt": value, "status": "ok" if value is not None else "nodata", "source": source["meta"]["id"]}
        return {"version": data["version"], "units": "feet", "method": method,
                "resolutionFt": data.get("resolutionFt", 40), "samples": result}

    def tile(self, zoom, col, row, version=None):
        data = self.manifest()
        projection_path = configure_projection(self._projection_directory(data))
        from PIL import Image
        import numpy as np
        import rasterio
        from rasterio.vrt import WarpedVRT
        from rasterio.enums import Resampling
        from rasterio.transform import from_bounds
        if version is not None and version != data["version"]:
            raise ValueError("The bathymetry version changed. Refresh the page.")
        if not 0 <= zoom <= data["tiles"]["maxZoom"] or not 0 <= col < 2**zoom or not 0 <= row < 2**zoom:
            raise ValueError("Tile is outside the local bathymetry pyramid.")
        key = (data["version"], zoom, col, row)
        with self._lock:
            cached = self._tile_memory.get(key)
            if cached is not None:
                self._tile_memory.move_to_end(key)
                return cached
        cached = self._cached_tile(data, zoom, col, row)
        if cached is not None:
            return self._remember_tile(key, cached)
        span = 2 * WORLD / 2**zoom
        left, top = -WORLD+col*span, WORLD-row*span
        bounds = (left, top-span, left+span, top)
        values = np.zeros((TILE_SIZE, TILE_SIZE), dtype="uint8")
        for source in self._handles(data):
            b = source["meta"]["boundsMercator"]
            if b[0]-100 >= bounds[2] or b[2]+100 <= bounds[0] or b[1]-100 >= bounds[3] or b[3]+100 <= bounds[1]:
                continue
            with rasterio.Env(**({"PROJ_DATA":projection_path} if projection_path else {})), WarpedVRT(source["shade"], crs="EPSG:3857", transform=from_bounds(*bounds, TILE_SIZE, TILE_SIZE),
                           width=TILE_SIZE, height=TILE_SIZE, src_nodata=0, nodata=0,
                           resampling=Resampling.bilinear, warp_mem_limit=64) as vrt:
                tile = vrt.read(1)
                take = (values == 0) & (tile != 0)
                values[take] = tile[take]
        rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype="uint8")
        t = np.maximum(values.astype("float32")-1, 0)/254
        for channel, (dark, light) in enumerate(zip((54, 38, 25), (223, 199, 161))):
            rgba[:, :, channel] = np.rint(dark+(light-dark)*t).astype("uint8")
        rgba[:, :, 3] = np.where(values > 0, 255, 0)
        stream = io.BytesIO()
        Image.fromarray(rgba).save(stream, format="PNG", compress_level=3)
        payload = stream.getvalue()
        self._store_tile(data, zoom, col, row, payload)
        return self._remember_tile(key, payload)

    def _remember_tile(self, key, payload):
        with self._lock:
            self._tile_memory[key] = payload
            if len(self._tile_memory) > 256:
                self._tile_memory.popitem(last=False)
        return payload
