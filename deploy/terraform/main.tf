locals {
  services = toset([
    "storage.googleapis.com", "iam.googleapis.com", "run.googleapis.com",
    "artifactregistry.googleapis.com", "secretmanager.googleapis.com", "cloudscheduler.googleapis.com",
    "iamcredentials.googleapis.com", "sts.googleapis.com"
  ])
  buckets = toset(["data", "bathymetry", "tiles"])
  labels  = { application = "bluewater", environment = var.environment }
}

resource "google_project_service" "api" {
  for_each           = local.services
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "data" {
  for_each                    = local.buckets
  name                        = "${var.project_id}-bw-${var.environment}-${each.value}"
  project                     = var.project_id
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = local.labels
  versioning { enabled = true }
  soft_delete_policy { retention_duration_seconds = 604800 }
  lifecycle_rule {
    action { type = "Delete" }
    condition {
      age        = 30
      with_state = "ARCHIVED"
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}

resource "google_service_account" "web" {
  account_id   = "bw-${var.environment}-web"
  display_name = "Bluewater ${var.environment} web"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "worker" {
  account_id   = "bw-${var.environment}-worker"
  display_name = "Bluewater ${var.environment} refresh worker"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "publisher" {
  account_id   = "bw-${var.environment}-publisher"
  display_name = "Bluewater ${var.environment} initial data publisher"
  depends_on   = [google_project_service.api]
}

resource "google_project_iam_custom_role" "tile_cache" {
  role_id     = "bluewaterTileCache_${var.environment}"
  title       = "Bluewater ${var.environment} tile cache"
  description = "Read existing tiles and create immutable tiles; no overwrite or delete."
  permissions = ["storage.objects.get", "storage.objects.create"]
  depends_on  = [google_project_service.api]
}

resource "google_storage_bucket_iam_member" "web_reader" {
  for_each = toset(["data", "bathymetry"])
  bucket   = google_storage_bucket.data[each.value].name
  role     = "roles/storage.objectViewer"
  member   = "serviceAccount:${google_service_account.web.email}"
}

resource "google_storage_bucket_iam_member" "web_tiles" {
  bucket = google_storage_bucket.data["tiles"].name
  role   = google_project_iam_custom_role.tile_cache.name
  member = "serviceAccount:${google_service_account.web.email}"
}

resource "google_storage_bucket_iam_member" "worker_data" {
  bucket = google_storage_bucket.data["data"].name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.worker.email}"
}

resource "google_storage_bucket_iam_member" "initial_publisher" {
  for_each = local.buckets
  bucket   = google_storage_bucket.data[each.value].name
  role     = "roles/storage.objectUser"
  member   = "serviceAccount:${google_service_account.publisher.email}"
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "bluewater-${var.environment}"
  description   = "Bluewater versioned web and worker images"
  format        = "DOCKER"
  labels        = local.labels
  depends_on    = [google_project_service.api]
}
