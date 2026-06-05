# Only HSTS is added at the edge. The rest of the security headers (and the
# per-host/per-path CSP + X-Frame-Options logic) are emitted by the Caddy
# sidecar + the portal itself, which must stay authoritative — a blanket
# CloudFront security-headers policy would, e.g., put X-Frame-Options DENY on
# the child-app responses and break the iframe embedding. Caddy omits HSTS in
# HTTP_ONLY mode by design (the fronting TLS terminator owns it), so we add it
# here.
resource "aws_cloudfront_response_headers_policy" "hsts" {
  name = "${local.name}-hsts"
  security_headers_config {
    strict_transport_security {
      access_control_max_age_sec = 31536000
      include_subdomains         = true
      preload                    = false
      override                   = true
    }
  }
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = local.name
  aliases         = local.cert_sans
  price_class     = "PriceClass_100"
  web_acl_id      = aws_wafv2_web_acl.main.arn

  origin {
    domain_name = aws_lb.main.dns_name
    origin_id   = "alb"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
    # Secret the ALB checks (see alb.tf) so the origin is reachable only via
    # this distribution.
    custom_header {
      name  = "X-Origin-Verify"
      value = random_password.origin_verify.result
    }
  }

  default_cache_behavior {
    target_origin_id           = "alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    compress                   = true
    cache_policy_id            = data.aws_cloudfront_cache_policy.caching_disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.hsts.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
}
