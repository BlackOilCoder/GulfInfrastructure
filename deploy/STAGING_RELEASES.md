# Private staging and GitHub releases

Cloud Run service: `bw-staging-web`, project `bluewater-512614`, Dallas
(`us-south1`). This service reads the existing staging cloud datasets and bathymetry.
App passwords and invitations remain deferred. Google Identity-Aware Proxy (IAP)
provides browser sign-in and checks an explicit access list before requests reach
the service. Cloud Run IAM permits the IAP service agent to invoke the backend;
there is no anonymous invocation grant.

## Open the private staging site

Open <https://bw-staging-web-yhlumqdgzq-vp.a.run.app/> in a browser and sign in with
`mjdionne@gmail.com`. A local server, Docker or a proxy is not needed for viewing.
Only that account is initially authorized. Other Google accounts are denied unless
added to the IAP access list.

Add future staging viewers to `staging_viewers` in Terraform and plan/apply.
Terraform manages the service-level IAP access list authoritatively, so changes
made only in Cloud Console can be removed by a later apply. The project has no
Google organization; its external OAuth client/brand is bootstrapped once in the
Google console, outside Terraform, without storing client secrets in the repo.
Keep that OAuth client available for future IAP use. Google sign-in alone does not
grant application access; the IAP list remains the authorization check.

The OAuth application is currently External / Testing, with `mjdionne@gmail.com`
as its sole test user. When adding another staging viewer, also add their Google
email under Google Auth Platform > Audience > Test users. OAuth test-user access
and the IAP authorization list must both allow them. OAuth credentials were
generated and saved directly by Google IAP; no client secret was downloaded.

The existing ports 8080 and 8081 still serve their separate local containers.
`staging_proxy.ps1` was used before IAP; its IAM token is no longer sufficient for
IAP browser access. Use the HTTPS website directly.

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
