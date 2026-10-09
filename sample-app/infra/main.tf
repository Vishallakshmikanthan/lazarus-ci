terraform {
  required_version = ">= 1.6.0"
}

provider "aws" {
  region = var.region
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-artifacts"
}
