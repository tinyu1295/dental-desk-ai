#!/usr/bin/env bash
# Build both service images, push them to their ECR repositories, and
# (if the ECS services already exist) force a new deployment on each to
# pick up the new image. Deliberately kept outside Terraform - a plain
# script is simpler to run repeatedly than modelling "build and push an
# image" as infrastructure state.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TERRAFORM_DIR="$REPO_ROOT/terraform"

PROJECT_NAME="dental-desk-ai"
CLUSTER_NAME="${PROJECT_NAME}-cluster"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
IMAGE_TAG="$(date +%Y%m%d%H%M%S)"

# name, source dir, terraform output, ECS service name
SERVICES=(
  "booking-service|$REPO_ROOT/services/booking_service|booking_service_ecr_repository_url|${PROJECT_NAME}-booking-service"
  "voice-gateway|$REPO_ROOT/services/voice_gateway|voice_gateway_ecr_repository_url|${PROJECT_NAME}-voice-gateway"
)

echo "==> Logging in to ECR"
ANY_ECR_REPO="$(terraform -chdir="$TERRAFORM_DIR" output -raw booking_service_ecr_repository_url)"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${ANY_ECR_REPO%%/*}"

for entry in "${SERVICES[@]}"; do
  IFS='|' read -r NAME SERVICE_DIR TF_OUTPUT ECS_SERVICE_NAME <<< "$entry"
  ECR_REPO_URL="$(terraform -chdir="$TERRAFORM_DIR" output -raw "$TF_OUTPUT")"

  echo "==> [$NAME] Building image (linux/amd64, matching the EC2 host's t2/x86_64 architecture)"
  docker build --platform linux/amd64 -t "$NAME" "$SERVICE_DIR"

  echo "==> [$NAME] Tagging as $ECR_REPO_URL:$IMAGE_TAG and :latest"
  docker tag "$NAME:latest" "$ECR_REPO_URL:$IMAGE_TAG"
  docker tag "$NAME:latest" "$ECR_REPO_URL:latest"

  echo "==> [$NAME] Pushing $ECR_REPO_URL:$IMAGE_TAG and :latest"
  docker push "$ECR_REPO_URL:$IMAGE_TAG"
  docker push "$ECR_REPO_URL:latest"

  echo "==> [$NAME] Checking whether the ECS service exists yet"
  if aws ecs describe-services --cluster "$CLUSTER_NAME" --services "$ECS_SERVICE_NAME" \
       --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
    echo "==> [$NAME] Forcing a new deployment on $ECS_SERVICE_NAME"
    aws ecs update-service --cluster "$CLUSTER_NAME" --service "$ECS_SERVICE_NAME" \
      --force-new-deployment --region "$AWS_REGION" >/dev/null
  else
    echo "==> [$NAME] ECS service not found yet - image pushed, nothing to redeploy."
  fi
done

echo "==> Done. Watch rollout with:"
echo "    aws ecs describe-services --cluster $CLUSTER_NAME --services ${PROJECT_NAME}-booking-service ${PROJECT_NAME}-voice-gateway --query 'services[*].deployments'"
