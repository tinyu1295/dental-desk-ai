resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"

  tags = {
    Name = "${var.project_name}-cluster"
  }
}

# Both services run as separate ECS tasks, colocated on the one EC2 host
# (ec2.tf), using bridge networking with distinct static host ports -
# mirrors docker-compose.yml's own 8000/8001 split exactly:
#   booking_service -> hostPort 8000, internal only (not in aws_security_group.ec2)
#   voice_gateway   -> hostPort 8001, reached from the internet via nginx
# A container on this host reaches the other over 127.0.0.1:<hostPort>,
# the same way any two processes on one machine would - no service
# discovery needed for a single-host deployment like this one.

resource "aws_ecs_task_definition" "booking_service" {
  family                   = "${var.project_name}-booking-service"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  execution_role_arn       = aws_iam_role.ecs_execution.arn

  container_definitions = jsonencode([
    {
      name              = "booking-service"
      image             = "${aws_ecr_repository.booking_service.repository_url}:latest"
      essential         = true
      memoryReservation = var.booking_service_task_memory_reservation
      cpu               = var.booking_service_task_cpu
      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]
      # Mirrors db.py's DATABASE_URL fallback: when DATABASE_URL itself
      # isn't set, it's assembled from these POSTGRES_* pieces instead -
      # which keeps the actual password out of a plaintext environment
      # value (it only ever exists as the `secrets` entry below).
      environment = [
        { name = "POSTGRES_USER", value = var.db_username },
        { name = "POSTGRES_HOST", value = aws_db_instance.app.address },
        { name = "POSTGRES_PORT", value = tostring(aws_db_instance.app.port) },
        { name = "POSTGRES_DB", value = var.db_name },
      ]
      secrets = [
        { name = "POSTGRES_PASSWORD", valueFrom = aws_ssm_parameter.postgres_password.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.booking_service.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-booking-service"
  }
}

resource "aws_ecs_service" "booking_service" {
  name            = "${var.project_name}-booking-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.booking_service.arn
  desired_count   = 1
  launch_type     = "EC2"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Sequences the instance existing (and having joined the cluster)
  # before either service tries to place a task on it.
  depends_on = [aws_instance.app]

  tags = {
    Name = "${var.project_name}-booking-service"
  }
}

resource "aws_ecs_task_definition" "voice_gateway" {
  family                   = "${var.project_name}-voice-gateway"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  execution_role_arn       = aws_iam_role.ecs_execution.arn

  container_definitions = jsonencode([
    {
      name              = "voice-gateway"
      image             = "${aws_ecr_repository.voice_gateway.repository_url}:latest"
      essential         = true
      memoryReservation = var.voice_gateway_task_memory_reservation
      cpu               = var.voice_gateway_task_cpu
      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = 8001
          protocol      = "tcp"
        }
      ]
      environment = [
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
        { name = "PUBLIC_BASE_URL", value = "https://${var.domain_name}" },
        { name = "MEDIA_STREAM_URL", value = "wss://${var.domain_name}/media-stream" },
        # Same-host loopback, matching booking_service's hostPort 8000
        # above - no Docker Compose service-name DNS on ECS, so this
        # replaces docker-compose.yml's "http://booking_service:8000".
        { name = "BOOKING_SERVICE_URL", value = "http://127.0.0.1:8000" },
      ]
      secrets = [
        { name = "AWS_ACCESS_KEY_ID", valueFrom = aws_ssm_parameter.aws_access_key_id.arn },
        { name = "AWS_SECRET_ACCESS_KEY", valueFrom = aws_ssm_parameter.aws_secret_access_key.arn },
        { name = "TWILIO_AUTH_TOKEN", valueFrom = aws_ssm_parameter.twilio_auth_token.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.voice_gateway.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-voice-gateway"
  }
}

resource "aws_ecs_service" "voice_gateway" {
  name            = "${var.project_name}-voice-gateway"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.voice_gateway.arn
  desired_count   = 1
  launch_type     = "EC2"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # Ordered after booking_service at the resource-creation level only -
  # ECS has no native "wait for the other task to be healthy" the way
  # docker-compose.yml's `depends_on: condition: service_healthy` does,
  # so a fresh deploy can have voice_gateway start slightly before
  # booking_service is actually ready to answer /tools/* calls. In
  # practice this self-heals: voice_gateway doesn't call booking_service
  # until the first live turn of an actual phone call, by which point
  # booking_service has almost always finished starting.
  depends_on = [aws_ecs_service.booking_service]

  tags = {
    Name = "${var.project_name}-voice-gateway"
  }
}
