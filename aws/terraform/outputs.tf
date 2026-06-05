output "portal_url" {
  description = "Public URL once DNS + the distribution are live."
  value       = "https://${var.domain}"
}

output "cloudfront_domain" {
  description = "CloudFront distribution domain (target of the DNS aliases)."
  value       = aws_cloudfront_distribution.main.domain_name
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "Push the portal image here (used by aws/scripts/deploy.sh)."
  value       = aws_ecr_repository.portal.repository_url
}

output "rds_endpoint" {
  value = aws_db_instance.main.address
}

output "s3_bucket" {
  value = aws_s3_bucket.data.id
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "ecs_service" {
  value = aws_ecs_service.portal.name
}

output "region" {
  description = "Deployment region (used by aws/scripts/deploy.sh)."
  value       = var.region
}
