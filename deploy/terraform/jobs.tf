variable "worker_image" {
  description = "Immutable Artifact Registry worker image URI, preferably pinned by digest."
  type        = string
  default     = ""
}

variable "refresh_schedules" {
  description = "Monthly and weekly times use Houston time; currents use UTC for an exact 12-hour interval."
  type        = map(object({ cron = string, time_zone = string }))
  default = {
    infra      = { cron = "0 3 1 * *", time_zone = "America/Chicago" }
    production = { cron = "0 4 * * 0", time_zone = "America/Chicago" }
    currents   = { cron = "0 0,12 * * *", time_zone = "Etc/UTC" }
  }
}

locals {
  refresh_jobs = var.worker_image == "" ? {} : {
    infra      = { args = ["cloud", "build", "--kind", "infra"], cpu = "1", memory = "2Gi", timeout = "3600s" }
    production = { args = ["cloud", "build", "--kind", "production", "--", "--incremental", "--recent-years", "3"], cpu = "2", memory = "8Gi", timeout = "10800s" }
    currents   = { args = ["cloud", "build", "--kind", "currents"], cpu = "2", memory = "8Gi", timeout = "3600s" }
  }
}

resource "google_service_account" "scheduler" {
  account_id   = "bw-${var.environment}-scheduler"
  display_name = "Bluewater ${var.environment} schedule invoker"
  depends_on   = [google_project_service.api]
}

resource "google_service_account" "currents" {
  account_id   = "bw-${var.environment}-currents"
  display_name = "Bluewater ${var.environment} currents refresh"
  depends_on   = [google_project_service.api]
}

resource "google_storage_bucket_iam_member" "currents_data" {
  bucket = google_storage_bucket.data["data"].name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.currents.email}"
}

resource "google_secret_manager_secret_iam_member" "currents_credentials" {
  secret_id = google_secret_manager_secret.copernicus.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.currents.email}"
}

resource "google_cloud_run_v2_job" "refresh" {
  for_each            = local.refresh_jobs
  name                = "bw-${var.environment}-refresh-${each.key}"
  location            = var.region
  deletion_protection = false
  labels              = local.labels
  template {
    task_count  = 1
    parallelism = 1
    template {
      service_account = each.key == "currents" ? google_service_account.currents.email : google_service_account.worker.email
      timeout         = each.value.timeout
      max_retries     = 1
      containers {
        image = var.worker_image
        args  = each.value.args
        resources {
          limits = { cpu = each.value.cpu, memory = each.value.memory }
        }
        env {
          name  = "BLUEWATER_STORAGE_BACKEND"
          value = "gcs"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "BLUEWATER_DATA_BUCKET"
          value = google_storage_bucket.data["data"].name
        }
        dynamic "env" {
          for_each = each.key == "currents" ? [true] : []
          content {
            name = "BLUEWATER_COPERNICUS_CREDENTIALS"
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.copernicus.secret_id
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_project_service.api, google_storage_bucket_iam_member.worker_data,
  google_storage_bucket_iam_member.currents_data, google_secret_manager_secret_iam_member.currents_credentials]
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  for_each = local.refresh_jobs
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_job.refresh[each.key].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "refresh" {
  for_each         = local.refresh_jobs
  name             = "bw-${var.environment}-refresh-${each.key}"
  region           = var.region
  description      = "Refresh and atomically publish Bluewater ${each.key} data"
  schedule         = var.refresh_schedules[each.key].cron
  time_zone        = var.refresh_schedules[each.key].time_zone
  paused           = false
  attempt_deadline = "180s"
  retry_config { retry_count = 0 }
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.refresh[each.key].id}:run"
    body        = base64encode("{}")
    headers     = { "Content-Type" = "application/json" }
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.scheduler]
}

output "refresh_jobs" {
  value = { for kind, job in google_cloud_run_v2_job.refresh : kind => {
    name = job.name, schedule = var.refresh_schedules[kind], region = job.location
  } }
}
