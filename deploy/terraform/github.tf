# Numeric GitHub IDs prevent reuse of a renamed/deleted repository or owner.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "bw-${var.environment}-github"
  display_name             = "Bluewater ${var.environment} GitHub"
  depends_on               = [google_project_service.api]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                      = "GulfInfrastructure staging"
  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository_id" = "assertion.repository_id"
  }
  attribute_condition = "assertion.repository_id == '1426301897' && assertion.repository_owner_id == '10898284' && assertion.ref == 'refs/heads/feature-experiments' && assertion.workflow_ref == 'BlackOilCoder/GulfInfrastructure/.github/workflows/deploy-staging.yml@refs/heads/feature-experiments' && assertion.event_name in ['push', 'workflow_dispatch']"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account" "github_deployer" {
  account_id   = "bw-${var.environment}-github"
  display_name = "Bluewater ${var.environment} GitHub deployer"
  depends_on   = [google_project_service.api]
}

resource "google_service_account_iam_member" "github_federation" {
  service_account_id = google_service_account.github_deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/1426301897"
}

resource "google_artifact_registry_repository_iam_member" "github_writer" {
  location   = var.region
  repository = google_artifact_registry_repository.images.repository_id
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_service_account_iam_member" "github_web_identity" {
  service_account_id = google_service_account.web.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_cloud_run_v2_service_iam_member" "github_deploy" {
  count    = var.web_image == "" ? 0 : 1
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web[0].name
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.github_deployer.email}"
}

resource "google_project_iam_custom_role" "release_operations" {
  role_id     = "bluewaterReleaseOperations_${var.environment}"
  title       = "Bluewater release operation status"
  permissions = ["run.operations.get"]
}

resource "google_project_iam_member" "github_operations" {
  project = var.project_id
  role    = google_project_iam_custom_role.release_operations.name
  member  = "serviceAccount:${google_service_account.github_deployer.email}"
}

output "github_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "github_deployer" {
  value = google_service_account.github_deployer.email
}
