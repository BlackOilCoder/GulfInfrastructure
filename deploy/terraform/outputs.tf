output "web_environment" {
  description = "Environment for the future Cloud Run service; contains no credentials."
  value = {
    BLUEWATER_STORAGE_BACKEND   = "gcs"
    BLUEWATER_DATA_BUCKET       = google_storage_bucket.data["data"].name
    BLUEWATER_BATHYMETRY_BUCKET = google_storage_bucket.data["bathymetry"].name
    BLUEWATER_TILE_BUCKET       = google_storage_bucket.data["tiles"].name
  }
}

output "service_accounts" {
  value = {
    web       = google_service_account.web.email
    worker    = google_service_account.worker.email
    publisher = google_service_account.publisher.email
  }
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
