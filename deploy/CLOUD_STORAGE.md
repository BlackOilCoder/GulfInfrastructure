# Stage 2: private cloud storage foundation

Configured for project `bluewater-512614`, staging in Dallas (`us-south1`).
The staging foundation is provisioned and the existing datasets and bathymetry
have been uploaded. See [STAGING_STATUS.md](STAGING_STATUS.md) for the active
release IDs and preview commands. Local Compose remains the default. Private Cloud
Run staging and GitHub releases are documented in [STAGING_RELEASES.md](STAGING_RELEASES.md).
Application sign-in remains deferred; Cloud Run IAM protects staging.

## Provisioned infrastructure

`deploy/terraform/` creates these resources in an existing billed project:

- Three regional private buckets: published data, bathymetry, and hillshade tiles.
- Uniform bucket access, enforced public-access prevention, versioning and seven-day
  soft deletion. Terraform refuses bucket destruction; noncurrent object generations
  expire after 30 days. Immutable releases themselves are not automatically deleted.
- Separate web, refresh-worker and initial-publisher service accounts, without keys.
- Web read access to datasets/rasters and create-only/read tile-cache access.
- Refresh-worker write access to the published-data bucket only.
- Initial-publisher access to all three buckets for controlled migration.
- A regional Artifact Registry repository for later container releases.
- A regional Copernicus secret, a separate currents identity, and a schedule invoker.
- When `worker_image` is configured, three Cloud Run refresh jobs and enabled
  Cloud Scheduler triggers. See [REFRESH_JOBS.md](REFRESH_JOBS.md).

With `web_image` configured, Terraform also creates the private staging website
and a repository-restricted GitHub deployment identity. No application user account
or invitation email is created. Storage, requests, scheduled triggers and compute
incur Google Cloud charges.

Prerequisites: Terraform >=1.6,<2, Google Cloud CLI, project billing, and an operator
permitted to enable APIs and manage storage, service accounts, IAM roles and the
Artifact Registry repository. Do not use downloaded service-account keys.

When ready, from `deploy/terraform`:

```text
gcloud auth application-default login
# Copy terraform.tfvars.example to terraform.tfvars; fill in the project ID/region.
terraform init
terraform plan -out=staging.tfplan
terraform apply staging.tfplan
terraform output -json web_environment
terraform output service_accounts
```

Use the actual project ID rather than the display name. Dallas (`us-south1`) is the
selected region. Use a separate Terraform
working directory/state for production. Preserve the local Terraform state securely
until a remote state backend is configured; state and plans are ignored by Git.
Commit `.terraform.lock.hcl` after initialization to retain the resolved provider.
Do not change the environment value in an existing staging state to production.

## Runtime selection

The new adapter accepts:

```text
BLUEWATER_STORAGE_BACKEND=gcs
BLUEWATER_DATA_BUCKET=<data bucket name>
BLUEWATER_BATHYMETRY_BUCKET=<bathymetry bucket name>
BLUEWATER_TILE_BUCKET=<tile bucket name>
```

Names have no `gs://` prefix. With the backend unset or `local`, existing paths and
MBTiles behavior remain in use. Keep both environments' settings and credentials
separate. In Google Cloud use the attached service account; locally use Application
Default Credentials. Never put credentials into the image, URLs, .env committed to
Git, or Terraform variables. Local Docker ADC testing will require an explicit
read-only credential-file mount; the existing Compose file does not forward host
Google credentials or switch to GCS automatically.

The GCS client obtains/refreshes short-lived credentials. Rasterio/GDAL reads private
COGs through `/vsigs/` with a request-scoped Authorization header. Only requested
raster blocks are read; full rasters are not staged at startup. GDAL's shared HTTP
block cache and the existing numeric/tile LRUs are bounded. Datum grids and matching
native PROJ databases are small runtime metadata stored in `/tmp`, not full rasters.
Hillshade and numerical sampling remain independent. No bucket mount or shared
SQLite database is used in cloud mode.

## Initial data migration tools

Install `deploy/requirements-worker.txt` in a Python 3.12 environment with read access
to the local data and bathymetry, then configure ADC and the three bucket variables
above. The operator needs initial-publisher permissions, either through their own
authorized identity or explicitly configured service-account impersonation.

These are **real uploads and active-release changes**, to run only after reviewing
the bucket/environment settings:

```text
python tools/publish_cloud.py seed-data --kind infra
python tools/publish_cloud.py seed-data --kind production
python tools/publish_cloud.py seed-data --kind currents
python tools/publish_cloud.py seed-data --kind contours
python tools/publish_cloud.py seed-bathymetry
```

Defaults read the current project data and saved bathymetry location. Override
`--directory` with the actual dataset folder, e.g. `data/production`, rather than
the parent `data/`. `--structures-file` selects the legacy platform metadata input
for the infra seed. Infrastructure seeds require platforms, pipelines, wells,
platform metadata and Talos assets together.

The data importer validates manifest references, infrastructure hashes, JSON,
PNG integrity and gzip consistency. It uploads only referenced published files,
never workbook files, source ZIP archives, download caches or arbitrary directories.
Cloud Storage client checksums verify transfers. New object names are created with
generation-match zero. Active pointers are updated with compare-and-swap against
the generation observed at run start. If another publication wins, this run fails
without replacing it. Partial uploads remain inactive and can be reviewed later.

The bathymetry importer uploads the referenced numeric/shade rasters and NOAA grid,
exports existing cached MBTiles rows to XYZ PNG objects, writes an immutable manifest,
then activates the package. It does not upload the source ZIPs, native Windows PROJ
databases or the SQLite file. A new cloud release ID separates its tile cache from
the local cache. Its `upstreamVersion` records the original installed grid version.
Seeding is currently a complete upload into a new package, not a resumable migration
across command invocations. Plan for multi-GB upload time and retained old versions.

## Cloud refresh entry points

After initial seeding:

```text
python tools/publish_cloud.py build --kind infra
python tools/publish_cloud.py build --kind infra -- pipelines
python tools/publish_cloud.py build --kind production
python tools/publish_cloud.py build --kind currents -- --days 5
python tools/publish_cloud.py build --kind contours
```

The worker image exposes the same commands through its `cloud` task, for example:

```text
docker compose run --rm worker cloud --help
```

The deployed Cloud Run Jobs use image arguments `cloud build --kind <kind>` and
attached service accounts. In GCS mode direct `worker snapshots` etc.
are rejected to avoid writing to an ephemeral `/data` without publication.

Builds hydrate a complete previous release into an isolated scratch directory,
run the existing builder there, validate all files, upload a new immutable release,
and switch the pointer last. Partial infra/production refreshes preserve the other
datasets. Existing cloud releases and previous current-frame folders are retained;
the legacy builders' cleanup only affects their isolated staging directories.
The wrapper preserves the current upstream data-selection rules. It does not yet
automate downloading platform-structures.js from BSEE.

Use `--work-dir` to select an existing scratch parent. Production/bathymetry rebuilds
may need substantial disk; Cloud Run's memory-backed filesystem must not be treated
as persistent disk. Scheduled production builds refresh recent years incrementally
to limit archive downloads. Renewable per-dataset GCS leases prevent overlapping
builds, and generation preconditions prevent lost pointer updates. Job sizing
still needs review after actual executions. Bathymetry refresh is a separate build followed
by `seed-bathymetry`, not a regular `build` kind.

## Browser compatibility and release retention

- `/data/manifest.json` rewrites snapshot file URLs to immutable infra release paths.
- Production, current and contour indexes expose the cloud release ID as `version`.
  Subsequent `?v=...` requests resolve that exact release, including older sessions.
- Unversioned manifest/index requests always resolve the active pointer and are not
  cached as immutable. The frontend routes and numeric API shapes remain unchanged.
- Dataset files stream through the API in bounded chunks. Buckets stay private;
  no signed URL is disclosed to browsers.
- Hillshade objects are created with generation preconditions; another instance
  winning the same tile is harmless. Tile write failure is logged without suppressing
  the rendered image. Bathymetry manifest changes are checked at most every 30 seconds.

Do not delete immutable releases still needed by users. An automated retention and
orphan cleanup policy needs a client-session retention decision; none is enabled
for these live objects. Public response cache headers match local stage 1 today;
the authentication stage must apply private cache policy and account-aware browser
cache cleanup before any private-user deployment is exposed.

## Deployment status and remaining validation

The staging foundation, Copernicus secret and three scheduled refresh jobs are
provisioned. The Google provider selection is recorded in `.terraform.lock.hcl`.
All dataset groups and bathymetry are seeded. The separate cloud-backed localhost
preview served HTTP 200, resolved the cloud infrastructure manifest, reported
readiness, returned three numeric depths from private COGs, and served a detailed
hillshade PNG. No automated suite, full route-profile check, publication-race check,
or 20-user performance measurement has been run. Native Terraform/Google CLI
installations are not required. The original port-8080 container remains unchanged.

On this Windows checkout, the helper uses the existing Docker installation:

```powershell
.\tools\cloud_infra.ps1 Login
.\tools\cloud_infra.ps1 Status
.\tools\cloud_infra.ps1 Plan
# Review the saved plan before provisioning:
.\tools\cloud_infra.ps1 Apply
.\tools\cloud_infra.ps1 Outputs
```

Google sign-in stays in the user's browser and terminal. Credentials are retained
in the local Docker volume `bluewater-gcloud`, never copied into the repository or
an image. The Terraform container mounts that volume read-only. The helper's Apply
action executes the saved staging plan; it creates actual cloud resources.

Before production promotion, validate attached service-account permissions, both
known elevation routes with hillshade off, cold/warm raster performance, overlapping
publication, old-session URLs after publication, and the 20-user workload. The local
cloud preview currently uses the operator's ADC; least-privilege service-account
runtime behavior still needs verification when Cloud Run services/jobs are deployed.

References:
- [GDAL authenticated Google Storage range reads](https://gdal.org/en/stable/user/virtual_file_systems.html#vsigs-google-cloud-storage-files)
- [Cloud Storage generation preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)
- [Bucket public access prevention](https://docs.cloud.google.com/storage/docs/public-access-prevention)
