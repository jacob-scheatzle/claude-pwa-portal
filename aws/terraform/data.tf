data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# CloudFront's origin-facing edge IP ranges, as a managed prefix list. The ALB
# security group allows ingress only from this list so the origin can't be hit
# directly, only through the distribution.
data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

# AWS-managed CloudFront policies. CachingDisabled because the portal is a
# dynamic, cookie-authenticated app; AllViewer so the original Host header (and
# cookies) reach the origin — the portal dispatches child apps on Host.
data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer"
}
