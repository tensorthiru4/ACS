terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.10, < 8.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 6.10, < 8.0"
    }
  }

  # Remote state in a GCS bucket you create once (free tier: 5 GB in us-central1):
  #   gsutil mb -l us-central1 -b on gs://<project>-tfstate && gsutil versioning set on gs://<project>-tfstate
  # then: terraform init -backend-config="bucket=<project>-tfstate"
  backend "gcs" {
    prefix = "acs"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {
  project_id = var.project_id
}
