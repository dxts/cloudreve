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
