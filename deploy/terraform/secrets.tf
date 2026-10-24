resource "google_secret_manager_secret" "copernicus" {
  secret_id = "bluewater-${var.environment}-copernicus"
  labels    = local.labels
  replication {
    user_managed {
      replicas { location = var.region }
    }
  }
  lifecycle { prevent_destroy = true }
  depends_on = [google_project_service.api]
}

output "copernicus_secret" {
  value = google_secret_manager_secret.copernicus.id
}
