locals {
  name = var.project

  # Per-app subdomain wildcard, derived from the portal domain. Child apps are
  # served at <slug>.apps.<domain>; this is the cert SAN + CloudFront alias.
  apps_wildcard = "*.apps.${var.domain}"

  # Names the ACM certs cover and CloudFront serves: the portal host and the
  # per-app subdomain wildcard.
  cert_sans = [var.domain, local.apps_wildcard]

  tags = {
    Project   = var.project
    ManagedBy = "terraform"
  }

  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}
