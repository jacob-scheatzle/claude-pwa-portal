#!/usr/bin/env bash
# Build the portal image for Fargate (linux/amd64), push it to ECR, and roll the
# ECS service. Reads everything it needs from terraform outputs.
#
# Usage:
#   aws/scripts/deploy.sh              # build + push + force a new ECS deployment
#   aws/scripts/deploy.sh --skip-ecs   # build + push only (used during bootstrap)
#
# Prereqs on your machine: docker (with buildx), the aws CLI (authenticated to
# the target account), and terraform — and `terraform apply` must have run at
# least far enough to create the ECR repo.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TF_DIR="$HERE/../terraform"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

SKIP_ECS=false
[ "${1:-}" = "--skip-ecs" ] && SKIP_ECS=true

cd "$TF_DIR"
ECR_URL="$(terraform output -raw ecr_repository_url)"
REGION="$(terraform output -raw region)"
REGISTRY="${ECR_URL%/*}" # strip the trailing /<repo-name>
TAG="$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo manual)"

echo "==> ECR login ($REGISTRY)"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Building + pushing $ECR_URL:{$TAG,latest} (linux/amd64, INSTALL_AWS=true)"
# Fargate is amd64; force the platform so this works from an arm64 Mac. The AWS
# image carries boto3 + psycopg (the [aws] extra) via the INSTALL_AWS build arg.
docker buildx build \
  --platform linux/amd64 \
  --build-arg INSTALL_AWS=true \
  -t "$ECR_URL:$TAG" \
  -t "$ECR_URL:latest" \
  --push \
  "$REPO_ROOT"

if [ "$SKIP_ECS" = true ]; then
  echo "==> Image pushed ($TAG). Skipping ECS deploy (--skip-ecs)."
  exit 0
fi

CLUSTER="$(terraform output -raw ecs_cluster)"
SERVICE="$(terraform output -raw ecs_service)"

# The task definition tracks :latest, so a forced new deployment re-pulls the
# image we just pushed. (Pin var.image_tag + terraform apply instead if you want
# immutable, sha-tagged deploys.)
echo "==> Forcing new deployment of $SERVICE"
aws ecs update-service \
  --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment --region "$REGION" >/dev/null

echo "==> Waiting for the service to stabilize (recreate deploy — brief downtime)"
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE" --region "$REGION"
echo "==> Done. Deployed $TAG to $SERVICE."
