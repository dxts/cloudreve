# The server and its volume were created by hand in the Hetzner console and are
# intentionally NOT managed here: they hold data, and this config should never be able
# to recreate them. They are read as data sources so the firewall and DNS can refer to
# them. Everything below is safe to destroy and apply again.

data "hcloud_server" "cloudreve" {
  name = var.server_name
}

locals {
  fqdn = "${var.subdomain}.${var.domain}"
  ipv4 = data.hcloud_server.cloudreve.ipv4_address
  # Hetzner assigns a /64; the host configures ::1 inside it.
  ipv6 = cidrhost(data.hcloud_server.cloudreve.ipv6_network, 1)
}

# ---------------------------------------------------------------------------------------
# Network-level firewall. Only SSH and HTTP(S) reach the host; Cloudreve's own port 5212
# is never published, Caddy fronts it inside the pod network.
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

resource "hcloud_firewall_attachment" "cloudreve" {
  firewall_id = hcloud_firewall.cloudreve.id
  server_ids  = [data.hcloud_server.cloudreve.id]
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
