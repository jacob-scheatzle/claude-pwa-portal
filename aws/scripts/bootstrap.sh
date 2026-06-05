#!/usr/bin/env bash
# One-time bring-up of the AWS stack. Resolves the chicken-and-egg between the
# ECS service and the image it pulls by creating ECR first, pushing an image,
# then applying the rest.
#
# Prereqs: terraform, docker (buildx), aws CLI (authenticated), and a
# terraform.tfvars with at least `domain` + `route53_zone_id`
# (see aws/terraform/terraform.tfvars.example).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$HERE/../terraform"

cd "$TF_DIR"
if [ ! -f terraform.tfvars ]; then
  echo "Create $TF_DIR/terraform.tfvars first (copy terraform.tfvars.example)." >&2
  exit 1
fi

echo "==> terraform init"
terraform init -input=false

echo "==> Creating the ECR repository first (so we have somewhere to push)"
terraform apply -input=false -auto-approve -target=aws_ecr_repository.portal

echo "==> Building + pushing the first image"
"$HERE/deploy.sh" --skip-ecs

echo "==> Applying the full stack (the ECS service starts with the image present)"
terraform apply -input=false -auto-approve

echo
echo "==> Bootstrap complete."
echo "    Portal URL:        $(terraform output -raw portal_url)"
echo "    CloudFront domain: $(terraform output -raw cloudfront_domain)"
echo
echo "Give the ECS service a minute to reach steady state and CloudFront a few"
echo "minutes to deploy, then open the portal URL and run the first-run wizard."
echo "Later updates are just: aws/scripts/deploy.sh"
