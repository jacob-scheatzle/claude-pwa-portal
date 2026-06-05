variable "region" {
  description = "AWS region for the regional resources (VPC, ECS, RDS, ALB)."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for all resources."
  type        = string
  default     = "pwa-portal"
}

variable "domain" {
  description = "Public hostname for the portal, e.g. portal.example.com. Child apps are served at <slug>.apps.<this domain>."
  type        = string
}

variable "route53_zone_id" {
  description = <<-EOT
    Route53 hosted zone ID that owns `domain`. Used to create the ACM DNS
    validation records and the portal + *.apps alias records automatically.
    Required for a one-shot `terraform apply`. If you manage DNS elsewhere,
    see aws/README.md for the manual-validation adaptation.
  EOT
  type        = string
}

variable "image_tag" {
  description = "Portal image tag in ECR to deploy."
  type        = string
  default     = "latest"
}

variable "caddy_image" {
  description = "Caddy sidecar image with the portal Caddyfiles baked in. Defaults to the published GHCR image; mirror it into ECR if you prefer to avoid the public pull."
  type        = string
  default     = "ghcr.io/jacob-scheatzle/claude-pwa-portal-caddy:latest"
}

variable "task_cpu" {
  description = "Fargate task vCPU units (256 = 0.25 vCPU, 512 = 0.5, 1024 = 1)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate task memory in MiB (must be a valid CPU/memory pairing)."
  type        = number
  default     = 1024
}

variable "db_instance_class" {
  description = "RDS Postgres instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB."
  type        = number
  default     = 20
}

variable "mcp_enabled" {
  description = "Value for the portal MCP_ENABLED env var. Blank = auto (on, image bundles the dep); 'false' disables /mcp; 'true' forces it."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}
