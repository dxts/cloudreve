variable "server_name" {
  description = "Name of the Hetzner Cloud server that runs Cloudreve."
  type        = string
  default     = "ubuntu-4gb-nbg1-1"
}

variable "domain" {
  description = "Base domain registered at Porkbun."
  type        = string
  default     = "offlike.net"
}

variable "subdomain" {
  description = "Host label under the base domain; the site is served at <subdomain>.<domain>."
  type        = string
  default     = "drive"
}

variable "ssh_allowed_cidrs" {
  description = "Source ranges allowed to reach SSH. Narrow this to your own address if it is stable."
  type        = list(string)
  default     = ["0.0.0.0/0", "::/0"]
}

variable "s3_location" {
  description = "Hetzner Object Storage location; fsn1, nbg1 or hel1. Same as the server keeps traffic free."
  type        = string
  default     = "nbg1"
}

variable "bucket_name" {
  description = "Bucket for Cloudreve blobs. Globally unique within the Hetzner location."
  type        = string
  default     = "drive-offlike-net"
}

variable "hetzner_s3_access_key" {
  description = "Hetzner Object Storage access key (Console -> Security -> S3 Credentials)."
  type        = string
  sensitive   = true
}

variable "hetzner_s3_secret_key" {
  description = "Hetzner Object Storage secret key."
  type        = string
  sensitive   = true
}

variable "dns_ttl" {
  description = "TTL for the DNS records, in seconds. Porkbun's minimum is 600."
  type        = number
  default     = 600
}
