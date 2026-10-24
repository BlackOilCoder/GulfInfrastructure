variable "project_id" {
  description = "Existing Google Cloud project with billing enabled."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Supply a valid Google Cloud project ID, not its display name."
  }
}

variable "region" {
  description = "Keep buckets, jobs and the eventual Cloud Run service in this region."
  type        = string
  default     = "us-south1"
}

variable "environment" {
  description = "Use separate state directories and buckets for staging and production."
  type        = string
  default     = "staging"
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "Environment must be staging or production."
  }
}
