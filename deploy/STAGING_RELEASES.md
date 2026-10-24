# Private staging and GitHub releases

Cloud Run service: `bw-staging-web`, project `bluewater-512614`, Dallas
(`us-south1`). This service reads the existing staging cloud datasets and bathymetry.
App passwords and invitations remain deferred. Cloud Run IAM requires an authorized
Google identity for every request; the service has no anonymous invocation grant.

## Open the private staging site

Start Docker Desktop, then run in PowerShell from the project folder:

```powershell
.\tools\staging_proxy.ps1
```

Leave that terminal open and visit <http://127.0.0.1:8082/>. Ctrl+C stops the proxy.
Port 8082 serves the actual Cloud Run site through an authenticated local proxy.
The existing ports 8080 and 8081 still serve their separate local containers.

The proxy uses the Google account signed into the `bluewater-gcloud` Docker volume.
Sign in again using `cloud_infra.ps1 Login` if needed. Only the operator
`mjdionne@gmail.com` is initially granted invocation. Add future staging viewers
to `staging_viewers` in Terraform and plan/apply. Each viewer needs their own Google
login/proxy; the raw Cloud Run URL is not a browser sign-in page or a public demo.

Cloud IAM protects access before requests reach the application. Responses use
`Cache-Control: private, no-store`. The frontend still uses IndexedDB for dataset
caching; avoid using an authorized staging browser profile on a shared computer.

## Release workflow

Push application changes to `feature-experiments` in
`BlackOilCoder/GulfInfrastructure`. `.github/workflows/deploy-staging.yml` builds
the web image, tags it with the commit SHA, pushes it to Artifact Registry, resolves
the immutable digest and updates the existing Cloud Run service. Cloud Run promotes
the new revision after startup succeeds. Watch the repository's **Actions** tab.
The workflow can also be dispatched manually on that branch once present in the
default branch. Data-only and documentation-only changes do not trigger a web build.

GitHub uses Workload Identity Federation and a dedicated deployment service account;
no Google key or password is stored in GitHub. Trust is restricted by numeric owner
and repository IDs, branch, workflow path and event type. Pull request workflows
cannot use this deployment identity. No GitHub repository variables are required.

The deployment account can publish images, update this service, use its runtime
identity and read deployment-operation status. It cannot grant public access,
change bucket IAM or manage refresh schedules. Protect the release branch and
review changes to its workflow. Public repository source remains public; private
Cloud Run does not change repository visibility or hide files already in Git.

Terraform owns service settings and IAM. GitHub owns the deployed image field,
which Terraform ignores after initial provisioning. Infrastructure changes still
use the local reviewed plan/apply workflow. `main` does not deploy production;
production promotion and a public demo are separate later stages. Worker images
and Cloud Run Jobs remain managed separately by Terraform.

## Rollback and limits

List revisions in Cloud Run's console and direct 100% traffic to the previous
healthy revision, or deploy its prior image digest with `gcloud run services update`.
A code rollback does not roll back dataset active pointers. Retain old images and
dataset releases until a retention policy is agreed.

The service scales from zero to three instances, with two CPUs, 2 GiB memory and
16 concurrent requests per instance. These are starting limits, not load-tested
capacity or a spending cap. Startup/liveness probes use `/healthz`; source-data
readiness is separately available at `/readyz`. No test suite or full browser
validation is run by this initial release workflow. Monitoring, budget alerts and
remote Terraform state remain follow-up work.

References: [Cloud Run developer proxy](https://docs.cloud.google.com/run/docs/authenticating/developers),
[GitHub authentication to Google Cloud](https://github.com/google-github-actions/auth).
