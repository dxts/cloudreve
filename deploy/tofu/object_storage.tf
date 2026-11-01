# ---------------------------------------------------------------------------------------
# Hetzner Object Storage (S3-compatible, Ceph) holds the file blobs. Cloudreve talks to it
# as an "S3" storage policy with presigned direct uploads and downloads, so clients move
# bytes to and from the bucket themselves; the cx23 only handles metadata, thumbnails and
# previews. The SQLite database stays on the volume.
#
# Hetzner exposes buckets only through the S3 API (no hcloud API, no hcloud CLI), so this
# uses the AWS provider with a custom endpoint. S3 credentials are created in the Hetzner
# Console under Security -> S3 Credentials and passed in via TF_VAR_* (see ../.envrc.example).
# ---------------------------------------------------------------------------------------

provider "aws" {
  alias = "hetzner"

  region     = var.s3_location
  access_key = var.hetzner_s3_access_key
  secret_key = var.hetzner_s3_secret_key

  # Not AWS: skip everything that assumes it is.
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    s3 = "https://${var.s3_location}.your-objectstorage.com"
  }
}

resource "aws_s3_bucket" "blobs" {
  provider = aws.hetzner
  bucket   = var.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

# Browsers upload straight to the bucket from the web UI, so the bucket must answer CORS
# preflights from the site origin. Cloudreve's admin panel has a "set CORS" button that
# would write a wildcard origin here; leave it unpressed, tofu owns this.
resource "aws_s3_bucket_cors_configuration" "blobs" {
  provider = aws.hetzner
  bucket   = aws_s3_bucket.blobs.id

  cors_rule {
    allowed_origins = ["https://${local.fqdn}"]
    allowed_methods = ["GET", "PUT", "POST", "DELETE", "HEAD"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# Abandoned chunked uploads leave multipart parts behind that are billed as storage.
resource "aws_s3_bucket_lifecycle_configuration" "blobs" {
  provider = aws.hetzner
  bucket   = aws_s3_bucket.blobs.id

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
