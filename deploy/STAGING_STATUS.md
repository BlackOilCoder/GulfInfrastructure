# Dallas staging storage — October 24, 2026

Project: `bluewater-512614` · Region: `us-south1` · Environment: `staging`.

The initial foundation created 19 resources. All three buckets have uniform bucket access,
enforced public-access prevention, versioning and seven-day soft deletion.

| Resource | Name |
|---|---|
| Published datasets | `bluewater-512614-bw-staging-data` |
| Numeric/shade rasters and datum grid | `bluewater-512614-bw-staging-bathymetry` |
| Hillshade PNG cache | `bluewater-512614-bw-staging-tiles` |
| Image registry | `us-south1-docker.pkg.dev/bluewater-512614/bluewater-staging` |

Web, worker and initial-publisher service accounts were created with the bucket
permissions defined in Terraform. No service-account keys were generated.

## Initial active releases

| Group | Release | Published content |
|---|---|---|
| Infrastructure | `20261024T150154-70047d0f81eb` | 7 catalogue entries; gzip companions where available |
| Production | `20261024T150214-82aa9a543f6d` | 577 entries including the index and well/platform shards |
| Currents | `20261024T150246-881b77339aa2` | 81 entries including the manifest and frames |
| Contours | `20261024T150315-28676c870248` | Index and 1,040 contour tiles |
| Bathymetry | `20261024T150335-84ef26350f60` | Four COG rasters, NOAA grid and manifest; 2,582 seeded hillshade PNGs |

The original bathymetry source version is preserved as `upstreamVersion`.
The Talos checksum/byte count in the local infrastructure manifest was refreshed
to match its existing JSON file; asset contents were not changed. Removed legacy
monolithic contour entries are omitted from cloud infrastructure releases. Contours
are supplied by their independent tile release.

## Local cloud-backed preview

Open <http://127.0.0.1:8081/>. It reads private cloud data and rasters, using the
operator's ADC in the `bluewater-gcloud` Docker volume. No local dataset or raster
folders are mounted into this preview. The original local site on port 8080 is a
separate container and remains available.

```powershell
docker compose -f deploy/compose.cloud-preview.yaml up --build -d web
docker compose -f deploy/compose.cloud-preview.yaml logs -f web
docker compose -f deploy/compose.cloud-preview.yaml stop web
```

This operator-only localhost preview runs as root to read the root-owned ADC
volume, with capabilities dropped and a read-only root filesystem. This is not the
Cloud Run execution identity; the private Cloud Run staging service uses the image's
nonroot user and an attached service account. No public website is deployed.

## Observed checks

- Website HTTP 200 and active infra manifest resolved immutable cloud release URLs.
- Application, data and bathymetry readiness all true.
- Numeric API returned valid depths of 7,555 ft, 7,378 ft and 6,249 ft at three
  requested positions, using the cloud package.
- A detailed zoom-14 hillshade request returned HTTP 200 and a 65,494-byte PNG.
- All three buckets reported Dallas and enforced public-access prevention.

These are basic operation checks, not full-route, concurrency, load, or rollback
tests. The preview uses operator credentials; service-account runtime checks and
the remaining validation are listed in CLOUD_STORAGE.md.

## Operations

```powershell
.\tools\cloud_infra.ps1 Status
.\tools\cloud_infra.ps1 Plan
.\tools\cloud_infra.ps1 Outputs
# Real republishing actions; use only when a new seed is intended:
.\tools\cloud_seed.ps1 -Kind infra
```

`cloud_seed.ps1` defaults to all groups, including a complete new bathymetry upload.
Do not rerun it just to inspect status. It uses the built worker image; rebuild the
worker after code changes. Terraform state remains in ignored files under
`deploy/terraform`; retain it for future management. A remote state backend and
GitHub deployment identity are not configured yet.

## Scheduled refreshes

Three Cloud Run Jobs and enabled Cloud Scheduler triggers are provisioned in
Dallas. Infrastructure runs on the first of each month at 3 a.m. Houston time;
production runs Sundays at 4 a.m. Houston time; currents and depth profiles run at
00:00 and 12:00 UTC. The currents job uses Secret Manager credentials entered
locally by the operator. The jobs have not yet completed an end-to-end refresh.
See [REFRESH_JOBS.md](REFRESH_JOBS.md) for commands, sizing and manual datasets.

Joined platform metadata retains its existing source until its download is
automated. Bathymetry/contours remain manual; Talos publication follows workbook
replacement. No additional schedule was created for these manual inputs.

## Private web staging

`bw-staging-web` is deployed in Dallas with Google IAP browser sign-in, the web
runtime service account and the private cloud buckets. Open its HTTPS URL directly
using the authorized Google account. `feature-experiments` web changes deploy through the GitHub
workflow. See [STAGING_RELEASES.md](STAGING_RELEASES.md) for access and rollback.

App login/invitations and the administration panel are deferred. A public demo,
production deployment, execution monitoring/alerts and remote Terraform state remain
separate follow-up work.
