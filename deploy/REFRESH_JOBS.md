# Staging refresh jobs

Project `bluewater-512614`, Dallas (`us-south1`). Terraform manages the Cloud Run
Jobs, Cloud Scheduler triggers, identities and secret access in `terraform/jobs.tf`.

## Schedule

| Dataset | Job suffix | Schedule |
|---|---|---|
| Platforms, pipelines and wells | `infra` | First day of the month, 3 a.m. America/Chicago |
| Production | `production` | Sunday, 4 a.m. America/Chicago |
| Ocean currents and depth profiles | `currents` | 00:00 and 12:00 UTC, every 12 hours |
| Joined platform metadata | Included in infrastructure | Existing source retained; upstream download not automated yet |
| Bathymetry and depth contours | No scheduled job | Manual refresh when BOEM publishes changes |
| Talos assets | No scheduled job | Convert replacement workbook locally and publish infrastructure |

Currents run at 7 a.m./7 p.m. Houston daylight time and 6 a.m./6 p.m. standard
time. UTC keeps the interval at 12 hours through daylight-saving transitions.
The deployed jobs are named `bw-staging-refresh-<suffix>`.

## Manage jobs from PowerShell

Start Docker Desktop and use the existing Google login in `bluewater-gcloud`:

```powershell
.\tools\cloud_jobs.ps1 Status
.\tools\cloud_jobs.ps1 Logs -Kind currents
# Starts a real refresh and publication; returns before completion:
.\tools\cloud_jobs.ps1 Run -Kind infra
.\tools\cloud_jobs.ps1 Pause -Kind production
.\tools\cloud_jobs.ps1 Resume -Kind production
```

Pause stops future triggers; it does not cancel an execution already running.
Terraform declares schedules enabled, so a later apply can undo an operational
pause. For a lasting schedule change, edit `refresh_schedules` in the ignored
`deploy/terraform/terraform.tfvars`, then run `cloud_infra.ps1 Plan`, review it,
and run `cloud_infra.ps1 Apply`.

Scheduler success means Cloud Run accepted an execution. Check Cloud Run execution
status and logs to confirm the refresh finished; Scheduler does not wait for the
data build. No end-to-end refresh execution was triggered as part of provisioning.

## Credentials and permissions

The currents job alone receives the latest version of
`bluewater-staging-copernicus` from Google Secret Manager. Its separate service
account can read that secret and write the staging data bucket. Infrastructure
and production use the worker account without access to Copernicus credentials.
Scheduler has job-scoped invocation permission; no service-account keys are used.

To rotate the Copernicus login, run this in a local terminal:

```powershell
.\tools\set_copernicus_secret.ps1
```

It prompts for the username and a hidden password, sends the JSON through standard
input to Secret Manager, and creates a new version. Credentials are not saved to
a repository file or Terraform state. Future executions use the latest version.

## Publication and sizing

Each build hydrates the previous release into temporary storage, refreshes its
sources, validates the result, uploads immutable files, and switches the active
pointer last. Errors leave the previous release active. A renewable per-dataset
GCS lease avoids overlapping builds; generation checks prevent lost pointer updates.

| Job | CPU | Memory including temporary files | Task timeout |
|---|---|---|---|
| Infrastructure | 1 | 2 GiB | 1 hour |
| Production | 2 | 8 GiB | 3 hours |
| Currents | 2 | 8 GiB | 1 hour |

Each job runs one task with one retry. Production refreshes the latest three years
of well records while retaining older history, and refreshes platform production.
Older well-history corrections require a manual full rebuild. Temporary archives
and staging files are ephemeral; size and duration should be reviewed after the
first executions. Failure alerting and automatic release cleanup are not configured.

The worker image is pinned by digest in `terraform.tfvars`. For code updates,
rebuild/push the worker, update `worker_image`, and plan/apply Terraform. Publishing
new data does not require rebuilding the web image.

## Manual datasets

After rebuilding bathymetry locally, publish it with
`cloud_seed.ps1 -Kind bathymetry`. Contours use their separate build/publication
workflow. After converting a replacement Talos workbook into the existing asset
JSON and updating the infrastructure manifest, publish with
`cloud_seed.ps1 -Kind infra`. These commands change the active staging release.
The monthly infrastructure job preserves the current Talos and joined platform
metadata inputs; it does not fetch a workbook or download the BSEE metadata source.

Cloud resources stay private. This stage does not deploy the web service,
application sign-in or the user invitation/admin panel.
