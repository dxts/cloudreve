terraform {
  required_version = ">= 1.8"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.19"
    }
    porkbun = {
      source  = "cullenmcdermott/porkbun"
      version = "~> 0.3"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# Both providers read their credentials from the environment (see ../.envrc.example):
#   hcloud  -> HCLOUD_TOKEN
#   porkbun -> PORKBUN_API_KEY, PORKBUN_SECRET_KEY
provider "hcloud" {}
provider "porkbun" {}
