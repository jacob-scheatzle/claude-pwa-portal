# Two certs covering the same names (portal host + *.apps wildcard): one in the
# deployment region for the ALB, one in us-east-1 for CloudFront.
resource "aws_acm_certificate" "alb" {
  domain_name               = var.domain
  subject_alternative_names = [local.apps_wildcard]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_acm_certificate" "cloudfront" {
  provider                  = aws.us_east_1
  domain_name               = var.domain
  subject_alternative_names = [local.apps_wildcard]
  validation_method         = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# ACM uses the same DNS validation CNAME for a given domain name across regions
# in one account, so a single set of records validates both certs. Keyed by
# domain_name (the portal host + the wildcard => 2 records).
resource "aws_route53_record" "validation" {
  for_each = {
    for o in aws_acm_certificate.cloudfront.domain_validation_options :
    o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  }
  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "alb" {
  certificate_arn         = aws_acm_certificate.alb.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}

resource "aws_acm_certificate_validation" "cloudfront" {
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront.arn
  validation_record_fqdns = [for r in aws_route53_record.validation : r.fqdn]
}
