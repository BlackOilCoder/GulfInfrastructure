# Stage 1: portable local containers

This package runs the existing website and a production FastAPI/Uvicorn HTTP adapter
in a Linux container, with a separate image for the existing Python builders.
This stage uses local mounted data. Cloud Storage, scheduled cloud jobs, login,
invitations, administration, and GitHub deployment are subsequent stages.

## Requirements

- Docker Engine/Desktop with Linux containers and Docker Compose 2.24 or newer.
- Existing `data/`, `platform-structures.js`, and an installed bathymetry package.
- Permission for Docker Desktop to access those host folders.
- For native Python use of the new server: Python 3.12 and the dependencies in
  `deploy/requirements-web.txt`. The previous `.bat` launcher remains available.

The Compose website binds only to `127.0.0.1`. Authentication is not implemented
yet; this stage must stay local while private-access work is pending.

## Start on this Windows checkout

From the project folder, prepare local configuration once:

```powershell
.venv-bathymetry\Scripts\python.exe tools\configure_containers.py
docker compose up --build -d web
```

Open <http://127.0.0.1:8080/>. This uses a different port from the existing native
preview at 8765, so the two can coexist.

The configuration helper reads the saved bathymetry location and writes an ignored
`.env`. It refuses to overwrite an existing `.env`; edit that file to change paths,
port, or credentials. Alternatively copy `.env.example` to `.env` and fill it in.
Do not copy `.bluewater-local.json` into an image: its Windows path is host specific.

Useful commands:

```text
docker compose logs -f web
docker compose ps
docker compose stop web
docker compose up --build -d web
docker compose down
```

Rebuild after source changes. Code is copied into images rather than live-mounted,
so local operation resembles a release deployment. No data is downloaded by web
startup. `docker compose down` preserves host data and the worker scratch volume.

## What is mounted

| Host input | Container path | Web access | Worker access |
|---|---|---|---|
| Published `data/` | `/data` | Read only | Read/write |
| `platform-structures.js` | `/inputs/platform-structures.js` | Read only | Read only |
| Installed bathymetry folder | `/bathymetry` | Read/write for MBTiles | Read/write |
| Named worker scratch volume | `/work` | None | Read/write |

The images exclude the generated datasets, bathymetry, credentials, `.git`, virtual
environments, and spreadsheets. Talos assets continue to use the existing generated
`data/talos-active-assets.json`; the workbook is not needed by the web container.

Containers run as user/group `10001:10001`. On a Linux host, grant that identity
read access to inputs, write access to the bathymetry cache, and write access to
worker output. Mounting a Windows Store-virtualized folder may not be accessible
to Docker Desktop: if necessary copy the bathymetry package to a normal local
folder and change `BLUEWATER_HOST_BATHYMETRY` in `.env`.

The NOAA coordinate grid is reused from the mounted package. Startup copies the
small PROJ databases from the installed Linux wheels to `/tmp/bluewater-proj`,
alongside that grid. It does not use potentially incompatible Windows PROJ databases
for calculations or modify the host projection files. The numeric rasters and
existing MBTiles cache are retained. Projection networking stays disabled.

## Run builders explicitly

Build the worker once:

```text
docker compose --profile jobs build worker
```

Examples (these commands download or update real data):

```text
docker compose run --rm worker snapshots
docker compose run --rm worker snapshots pipelines
docker compose run --rm worker production
docker compose run --rm worker currents
docker compose run --rm worker contours
docker compose run --rm worker bathymetry --rebuild-tiles
```

For argument documentation without downloads:

```text
docker compose run --rm worker snapshots --help
docker compose run --rm worker production --help
docker compose run --rm worker currents --help
```

The worker's default command is `snapshots --help`; simply building or starting the
image does not trigger a refresh. It starts a single task, returns its exit code,
and exits. No scheduler runs inside either container.

Currents credentials can be set in the ignored `.env` as
`BLUEWATER_COPERNICUS_USERNAME` and `BLUEWATER_COPERNICUS_PASSWORD`. These variables
are passed only to the worker. Quote values containing `$`, `#`, or spaces with
single quotes according to Compose .env syntax. They are not baked into an image
or passed on the command line. Cloud credentials will move to Secret Manager in
a later stage.

Production download archives are retained at `/work/production-cache`. Python
scratch files use `/work/tmp`, which is disk-backed in the local named volume;
large builds do not fill the web container's memory-backed `/tmp`.

These commands still use the current builders' publishing behavior. Run one
builder at a time, and use a separate copy of `data/` for experimental refreshes.
The existing snapshot/production publication and currents cleanup are not yet
safe for concurrent production readers across releases. Atomic versioned cloud
publishing, retention, and durable overlap locks belong to the next stages.
Platform metadata conversion still uses the supplied JS source; this stage does
not add an automated upstream download for that input.

## Configuration inside the containers

| Variable | Meaning |
|---|---|
| `PORT` | Web listening port; default 8080; honors Cloud Run's supplied value |
| `BLUEWATER_DATA_DIR` | Published data root; default in images `/data` |
| `BLUEWATER_BATHYMETRY_DIR` | Raster package root; default `/bathymetry` |
| `BLUEWATER_STRUCTURES_FILE` | Legacy metadata input/fallback JS file |
| `BLUEWATER_PRODUCTION_CACHE_DIR` | Download archive cache; worker default `/work/production-cache` |
| `BLUEWATER_PROJ_DIR` | Runtime projection files; default `/tmp/bluewater-proj` |
| `BLUEWATER_RASTER_CONCURRENCY` | Maximum simultaneous raster operations per web process; default 4 |
| `BLUEWATER_RELEASE` | Version exposed by `/healthz`; default `local` |

`BLUEWATER_HOST_*` variables in `.env` are Compose host mount settings, not paths
inside the container. Native builder scripts use the same data variables when
provided, falling back to the current project paths. The old development server
continues to serve the project folder; use the new adapter for custom data roots.

## HTTP behavior

The frontend and API URLs are unchanged:

- `/`, `/index.html`, and `/platform-structures.js`.
- `/data/...`: published JSON and PNG files, with gzip snapshot negotiation.
- `/api/bathymetry/status`.
- `/api/bathymetry/sample`: bounded JSON body, existing 1–5,000-point sampling.
- `/api/bathymetry/hillshade/{z}/{x}/{y}.png?v=VERSION`.
- `/healthz`: process liveness and release identifier; independent of missing data.
- `/readyz`: returns 200 only when the app, infrastructure manifest, and bathymetry
  package are present; otherwise 503 with individual checks.

Repository files and directories are not served. The adapter allows only the
explicit web assets and published data formats; hidden paths and source/download
cache folders are excluded. Existing no-cache manifest behavior, version-query
caching and precompressed snapshot delivery are preserved. Profile calculations
continue to use the existing bathymetry engine independently of hillshade.

The container health check uses liveness so missing data does not cause restart
loops. Readiness is a package-presence check, not an integrity or depth-sample test.

## Native launch of the new adapter (optional)

In a separate Python 3.12 virtual environment:

```text
python -m pip install -r deploy/requirements-web.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

On this host the bathymetry engine reads the existing saved local configuration.
Leave `BLUEWATER_PROJ_DIR` unset for this native launch so it uses the original
installed projection files. Container startup prepares Linux files automatically.

## Before deploying to Cloud Run

This is the portable runtime foundation, not the cloud deployment package yet.
Optional cloud storage adapters and publication tools are now prepared; see
[deploy/CLOUD_STORAGE.md](deploy/CLOUD_STORAGE.md) for provisioning and validation.
The next stages must validate them against GCS, add authentication and administration,
provision Cloud Run Jobs/Scheduler, and connect staging/production deployments.
Cloud Run cannot use these host bind mounts or the local worker scratch volume.

Direct dependency versions are pinned in `deploy/requirements-*.txt`; transitive
dependencies and the Python base-image digest are not yet locked. Capture those
after the first successful Linux build before publishing production releases.

The web image was built locally and its startup, HTTP response, liveness and
data readiness were checked. Both images include `libexpat1`, required by the
raster runtime on the slim Debian base. The worker image and complete application
workflows have not yet been exercised in containers.
