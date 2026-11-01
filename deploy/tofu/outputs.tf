output "fqdn" {
  value = local.fqdn
}

output "ipv4" {
  value = local.ipv4
}

output "ipv6" {
  value = local.ipv6
}

output "server_id" {
  value = hcloud_server.cloudreve.id
}

output "volume_mount" {
  value = "/mnt/HC_Volume_${hcloud_volume.data.id}"
}

output "firewall_id" {
  value = hcloud_firewall.cloudreve.id
}

# Values to enter in Cloudreve's admin panel when creating the S3 storage policy.
output "s3_policy" {
  value = {
    endpoint   = "https://${var.s3_location}.your-objectstorage.com"
    bucket     = aws_s3_bucket.blobs.id
    region     = var.s3_location
    path_style = false
    private    = true
  }
}
