"""Native-resolution raster sampling over authenticated GCS range reads."""
import hashlib
import logging
import os
import threading
import time

from tools.bathymetry import Bathymetry
from tools.projection import prepare_projection_files
from tools.storage import RELEASE, object_name

LOG = logging.getLogger("bluewater")


class CloudBathymetry(Bathymetry):
    def __init__(self, rasters, tiles, cache_directory=None):
        super().__init__(cache_directory or os.environ.get("BLUEWATER_METADATA_CACHE_DIR", "/tmp/bluewater-metadata"))
        self.rasters, self.tiles = rasters, tiles
        self._checked_at = 0
        self._manifest_lock = threading.RLock()

    def manifest(self):
        with self._manifest_lock:
            if self._manifest is not None and time.monotonic() - self._checked_at < 30:
                return self._manifest
            pointer, _ = self.rasters.pointer("active.json")
            if pointer is None:
                raise FileNotFoundError("No cloud bathymetry package has been published.")
            version = pointer["release"]
            if not RELEASE.fullmatch(version):
                raise ValueError("Invalid bathymetry release.")
            if self._manifest is None or self._manifest["version"] != version:
                data = self.rasters.json(f"packages/{version}/manifest.json")
                if data.get("schemaVersion") != 1 or data.get("version") != version or not data.get("sources"):
                    raise ValueError("Invalid cloud bathymetry manifest.")
                for source in data["sources"]:
                    for field in ("depth", "hillshade"):
                        key = object_name(source[field])
                        if not key.startswith(f"packages/{version}/"):
                            raise ValueError("Raster is outside its bathymetry package.")
                grid_meta = data["datumGrid"]
                key = object_name(grid_meta["file"])
                if not key.startswith(f"packages/{version}/"):
                    raise ValueError("Datum grid is outside its bathymetry package.")
                grid_bytes = self.rasters.read(key, maximum=32 * 1024 * 1024)
                if hashlib.sha256(grid_bytes).hexdigest() != grid_meta["sha256"]:
                    raise ValueError("Coordinate grid checksum mismatch.")
                root = self._projection_directory(data)
                root.mkdir(parents=True, exist_ok=True)
                grid = root / "us_noaa_conus.tif"
                grid.write_bytes(grid_bytes)
                prepare_projection_files(grid, root / "proj")
                with self._lock:
                    self._manifest = data
                    self._blocks.clear()
                    self._tile_memory.clear()
            self._checked_at = time.monotonic()
            return self._manifest

    def status(self):
        try:
            state = super().status()
            if state["ready"]:
                state["source"] = "BOEM cloud bathymetry"
                state["storage"] = "gcs"
            else:
                state["message"] = "Cloud bathymetry is unavailable; check publication and service-account permissions."
            return state
        except Exception:
            LOG.exception("Cloud bathymetry status failed")
            return {"ready": False, "message": "Cloud bathymetry is unavailable; check publication and service-account permissions."}

    def _projection_directory(self, data):
        return self.directory / data["version"]

    def _raster_path(self, name):
        return f"/vsigs/{self.rasters.bucket.name}/{object_name(name)}"

    def _environment(self):
        import rasterio
        return rasterio.Env(GDAL_HTTP_HEADERS=f"Authorization: Bearer {self.rasters.token()}",
                            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                            GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_TIMEOUT="60",
                            GDAL_HTTP_MAX_RETRY="2", GDAL_HTTP_RETRY_DELAY="1",
                            CPL_VSIL_CURL_CACHE_SIZE=16777216)

    def sample(self, points, method="nearest"):
        with self._environment():
            result = super().sample(points, method)
            result["source"] = "BOEM cloud bathymetry"
            return result

    def tile(self, zoom, col, row, version=None):
        with self._environment():
            return super().tile(zoom, col, row, version)

    def _cached_tile(self, data, zoom, col, row):
        from google.api_core.exceptions import NotFound
        try:
            return self.tiles.read(f"hillshade/{data['version']}/{zoom}/{col}/{row}.png", maximum=1024*1024)
        except NotFound:
            return None

    def _store_tile(self, data, zoom, col, row, payload):
        from google.api_core.exceptions import PreconditionFailed
        try:
            self.tiles.put_new(f"hillshade/{data['version']}/{zoom}/{col}/{row}.png", payload, "image/png")
        except PreconditionFailed:
            pass  # Another instance published this immutable tile first.
        except Exception:
            LOG.exception("Could not persist generated hillshade tile")
