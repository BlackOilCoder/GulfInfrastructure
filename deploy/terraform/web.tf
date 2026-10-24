variable "web_image" {
  description = "Initial immutable web image. Later image releases are managed by GitHub Actions."
  type        = string
  default     = ""
}

variable "staging_viewers" {
  description = "IAM principals allowed to invoke private staging through an authenticated proxy."
  type        = set(string)
  default     = ["user:mjdionne@gmail.com"]
}

resource "google_cloud_run_v2_service" "web" {
  count               = var.web_image == "" ? 0 : 1
  name                = "bw-${var.environment}-web"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = true
  invoker_iam_disabled = false
  labels              = local.labels
  template {
    service_account                  = google_service_account.web.email
    max_instance_request_concurrency = 16
    timeout                          = "300s"
    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }
    containers {
      image = var.web_image
      ports { container_port = 8080 }
      resources {
        limits            = { cpu = "2", memory = "2Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = {
          BLUEWATER_STORAGE_BACKEND   = "gcs"
          BLUEWATER_DATA_BUCKET       = google_storage_bucket.data["data"].name
          BLUEWATER_BATHYMETRY_BUCKET = google_storage_bucket.data["bathymetry"].name
          BLUEWATER_TILE_BUCKET       = google_storage_bucket.data["tiles"].name
          BLUEWATER_PRIVATE_CACHE     = "1"
          BLUEWATER_RASTER_CONCURRENCY = "4"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 0
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 30
      }
      liveness_probe {
        http_get { path = "/healthz" }
        period_seconds    = 30
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }
  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }
  depends_on = [google_storage_bucket_iam_member.web_reader, google_storage_bucket_iam_member.web_tiles]
}

resource "google_cloud_run_v2_service_iam_member" "viewer" {
  for_each = var.web_image == "" ? toset([]) : var.staging_viewers
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.web[0].name
  role     = "roles/run.invoker"
  member   = each.value
}

output "staging_url" {
  value = try(google_cloud_run_v2_service.web[0].uri, null)
}
