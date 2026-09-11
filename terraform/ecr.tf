# One repository per service - booking_service and voice_gateway are
# built from separate Dockerfiles (services/booking_service/Dockerfile,
# services/voice_gateway/Dockerfile) and deployed as separate ECS tasks,
# so they need separate images.

resource "aws_ecr_repository" "booking_service" {
  name                 = "${var.project_name}-booking-service"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${var.project_name}-booking-service"
  }
}

resource "aws_ecr_repository" "voice_gateway" {
  name                 = "${var.project_name}-voice-gateway"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Name = "${var.project_name}-voice-gateway"
  }
}

locals {
  ecr_lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "booking_service" {
  repository = aws_ecr_repository.booking_service.name
  policy     = local.ecr_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "voice_gateway" {
  repository = aws_ecr_repository.voice_gateway.name
  policy     = local.ecr_lifecycle_policy
}
