# ---------------------------------------------------------------------------------------
# Compute and storage. These were created in the Hetzner console and imported here; the
# definitions mirror the live objects. Both carry prevent_destroy and Hetzner-side delete
# protection, so a bad plan errors out instead of taking data with it. Any change that
# would force replacement (server_type is fine, image or location is not) must be done
# deliberately by lifting those guards.
# ---------------------------------------------------------------------------------------

resource "hcloud_server" "cloudreve" {
  name        = var.server_name
  server_type = "cx23"
  image       = "ubuntu-26.04"
  location    = "nbg1"

  firewall_ids = [hcloud_firewall.cloudreve.id]

  delete_protection  = true
  rebuild_protection = true
  backups            = false

  labels = {
    app = "cloudreve"
  }

  lifecycle {
    prevent_destroy = true
    # Create-time only; not readable back from the API after import.
    ignore_changes = [ssh_keys, user_data]
  }
}

resource "hcloud_volume" "data" {
  name      = "volume-nbg1-1"
  size      = 10
  server_id = hcloud_server.cloudreve.id

  delete_protection = true

  labels = {
    app = "cloudreve"
  }

  lifecycle {
    prevent_destroy = true
    # format/automount were applied at creation; the host mounts it via fstab.
    ignore_changes = [format, automount]
  }
}

locals {
  fqdn = "${var.subdomain}.${var.domain}"
  ipv4 = hcloud_server.cloudreve.ipv4_address
  # Hetzner assigns a /64; the host configures ::1 inside it.
  ipv6 = cidrhost(hcloud_server.cloudreve.ipv6_network, 1)
}

# ---------------------------------------------------------------------------------------
# Network-level firewall. Only SSH and HTTP(S) reach the host; Cloudreve's own port 5212
# is never published, Caddy fronts it inside the container network.
# ---------------------------------------------------------------------------------------

resource "hcloud_firewall" "cloudreve" {
  name = "cloudreve"

  rule {
    description = "SSH"
    direction   = "in"
    protocol    = "tcp"
    port        = "22"
    source_ips  = var.ssh_allowed_cidrs
  }

  rule {
    description = "HTTP (ACME challenges and redirect to HTTPS)"
    direction   = "in"
    protocol    = "tcp"
    port        = "80"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "HTTPS"
    direction   = "in"
    protocol    = "tcp"
    port        = "443"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  rule {
    description = "ICMP (ping, path MTU)"
    direction   = "in"
    protocol    = "icmp"
    source_ips  = ["0.0.0.0/0", "::/0"]
  }

  labels = {
    app = "cloudreve"
  }
}

# ---------------------------------------------------------------------------------------
# DNS at Porkbun. Caddy obtains the certificate via HTTP-01, so no DNS API access is
# needed at runtime; these records are the only coupling.
# ---------------------------------------------------------------------------------------

resource "porkbun_dns_record" "a" {
  domain  = var.domain
  name    = var.subdomain
  type    = "A"
  content = local.ipv4
  ttl     = tostring(var.dns_ttl)
  notes   = "cloudreve, managed by deploy/tofu"
}

resource "porkbun_dns_record" "aaaa" {
  domain  = var.domain
  name    = var.subdomain
  type    = "AAAA"
  content = local.ipv6
  ttl     = tostring(var.dns_ttl)
  notes   = "cloudreve, managed by deploy/tofu"
}
