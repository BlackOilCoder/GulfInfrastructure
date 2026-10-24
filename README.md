# Bluewater — local snapshot setup

## Portable web and worker containers

The first deployment stage adds a FastAPI/Uvicorn web adapter, separate Linux
web/worker images, and Docker Compose with mounted datasets and bathymetry.
See [DEPLOYMENT.md](DEPLOYMENT.md) for setup, configuration, worker commands,
and the remaining Cloud Run stages. Existing local commands below remain available.

The optional GCS adapters, immutable publication tools and private-bucket Terraform
foundation are described in [deploy/CLOUD_STORAGE.md](deploy/CLOUD_STORAGE.md).
The Dallas staging foundation is provisioned and seeded; see
[deploy/STAGING_STATUS.md](deploy/STAGING_STATUS.md) for resource names, active
versions, the cloud-backed localhost preview, and remaining validation.
Scheduled cloud refreshes and management commands are documented in
[deploy/REFRESH_JOBS.md](deploy/REFRESH_JOBS.md).
Private Cloud Run staging and automated GitHub web releases are described in
[deploy/STAGING_RELEASES.md](deploy/STAGING_RELEASES.md).

## Folder layout
    index.html
    platform-structures.js     (legacy source; still used as a fallback)
    data/                      (generated snapshots + manifest.json)
    tools/build_snapshots.py   (refreshes data/)
    tools/serve.py             (local server with gzip + cache headers)

## First run / refresh data
    python tools/build_snapshots.py              # platforms, wells, pipelines + structures
    python tools/build_snapshots.py pipelines    # refresh just one dataset
    python tools/build_snapshots.py --structures-only

When a new platform-structures.js is dropped in, run with --structures-only.

## Serve
    tools\start_bluewater.bat      # http://localhost:8000/

The launcher uses `.venv-bathymetry`, which includes the raster dependencies. You can
also run `.venv-bathymetry\Scripts\python tools\serve.py 8000` directly. A plain
`python -m http.server` does not provide the local bathymetry API.

## Local BOEM bathymetry

One-time installation (Python 3.10 or newer):

    tools\install_bathymetry.bat

This downloads BOEM's East and West **depth-in-feet** GeoTIFF archives (about 1.3 GB
compressed), verifies their ZIP integrity, and builds:

- Losslessly compressed numeric Cloud Optimized GeoTIFFs at the original resolution.
- Native-resolution hillshade rasters (north lighting, 45-degree altitude, 5x visual
  exaggeration). The numeric depths are never exaggerated.
- A brown hillshade tile cache in MBTiles, prefilled through zoom 11. Finer tiles
  through zoom 14 are generated locally on first use and retained on disk.
- NOAA's NAD27 CONUS correction grid, used locally with PROJ networking disabled.
- A versioned manifest with source hashes, coordinate systems and coverage bounds.

The default data location is `%LOCALAPPDATA%\Bluewater\bathymetry`, outside OneDrive
and Git. Allow several GB for the installed rasters, source archives and build
scratch space. To choose another location, set `BLUEWATER_BATHYMETRY_DIR` before
running both the installer and launcher, or supply `--directory` to the builder
and `--bathymetry-dir` to `serve.py`.
The installer remembers the selected folder in `.bluewater-local.json` (ignored by
Git), so the launcher can find it even if the shell's environment differs.

The map requests small local image tiles. **Show Elevation independently requests
native-resolution numeric samples**; the hillshade switch does not affect sampling.
Hillshade displays at every map scale when enabled. Points outside coverage or on
NoData cells remain gaps.
Profiles use nearest-cell sampling; the API also offers strict bilinear sampling
that does not interpolate across NoData. Excel profiles record the dataset version
and sampling method.

Manual refresh when BOEM republishes the data:

    tools\install_bathymetry.bat --refresh

A build writes a new version and switches the active manifest only after completion.
Failed builds leave the existing package active. Old versions and source downloads
are retained so they can be reviewed or removed later. To rebuild only the tile cache:

    tools\install_bathymetry.bat --rebuild-tiles

API routes on the same local server:

- `GET /api/bathymetry/status`: availability, version, units and coverage.
- `POST /api/bathymetry/sample`: JSON `{points:[[longitude,latitude],...],
  method:"nearest", version:"..."}`; 1–5,000 WGS 84 points, ordered results in feet
  positive downward. Missing depths are `null` with an explicit status.
- `GET /api/bathymetry/hillshade/{z}/{x}/{y}.png?v=VERSION`: cached 256-pixel tiles.

The bathymetry service makes no agency requests while the application is running.
The ArcGIS JavaScript SDK, basemaps and any live infrastructure fallback still use
their existing network sources.

Local regression checks (synthetic rasters; no agency downloads):

    .venv-bathymetry\Scripts\python tools\test_bathymetry.py

## How loading works
1. The browser fetches data/manifest.json (small, always revalidated).
2. For each dataset, if IndexedDB already holds the same sha256 version it is used directly;
   otherwise the versioned JSON file is downloaded and cached.
3. If the snapshot cannot be fetched, the last cached copy is used (offline); if there is
   none, that layer falls back to the live BOEM feature service.
4. Wells load when the Wells layer, Talos layer or a search needs them.
5. Platform Structures load in the background after the map has drawn.

Clear the cache: browser DevTools > Application > IndexedDB > bluewater-snapshots > Delete.
