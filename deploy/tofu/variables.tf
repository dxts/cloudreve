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

variable "dns_ttl" {
  description = "TTL for the DNS records, in seconds. Porkbun's minimum is 600."
  type        = number
  default     = 600
}
