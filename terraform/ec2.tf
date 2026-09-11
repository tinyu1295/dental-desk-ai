# Single EC2 host running the ECS agent (EC2 launch type, not Fargate) +
# nginx/certbot for TLS termination, fronting both containers. No ALB -
# a single instance with SSM Session Manager for access (no SSH/port 22)
# keeps this within a demo project's cost, at the price of a single point
# of failure that a real production deployment wouldn't accept.

data "aws_ssm_parameter" "ecs_ami" {
  # AWS-published, always-current ECS-optimized AL2023 x86_64 AMI - t2
  # instances are x86_64-only (see variables.tf's ec2_instance_type
  # comment), matches scripts/deploy.sh's --platform linux/amd64 build.
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

# --- IAM: lets the instance (not the containers) join the ECS cluster
# and be reachable for shell access without opening port 22 anywhere ---
data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ecs_instance" {
  name               = "${var.project_name}-ecs-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_instance_role" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

# SSM Session Manager instead of SSH - no port 22 / key pair needed at all.
resource "aws_iam_role_policy_attachment" "ecs_instance_ssm" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ecs_instance" {
  name = "${var.project_name}-ecs-instance"
  role = aws_iam_role.ecs_instance.name
}

# --- Security group: this instance is the only internet-facing endpoint
# in the whole setup (no ALB/CloudFront in front of it), so 80/443 are
# open to the world - same posture as any self-hosted nginx box. Neither
# container port (8000 for booking_service, 8001 for voice_gateway) is
# in this security group at all: nginx reaches voice_gateway over
# 127.0.0.1:8001, and voice_gateway reaches booking_service over
# 127.0.0.1:8000 - both loopback, never through this SG. No port 22 -
# SSM replaces it. ---
resource "aws_security_group" "ec2" {
  name        = "${var.project_name}-ec2"
  description = "HTTP/HTTPS from anywhere; no SSH (SSM Session Manager instead)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTP (nginx, and the ACME HTTP-01 challenge)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS (nginx, TLS-terminated)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ec2-sg"
  }
}

locals {
  # Proxies to voice_gateway (127.0.0.1:8001) only. There's no need for
  # an nginx-level block on /tools/* the way a single-service setup would
  # need one: booking_service's /tools/* routes live on a completely
  # separate container (hostPort 8000) that this proxy never forwards
  # to at all, and that isn't in aws_security_group.ec2 either - the
  # separation is architectural here, not an nginx rule someone could
  # forget to add.
  nginx_conf = templatefile("${path.module}/templates/nginx.conf.tpl", {
    domain_name = var.domain_name
  })
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.ecs_ami.value
  instance_type          = var.ec2_instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.ecs_instance.name

  user_data = templatefile("${path.module}/templates/user_data.sh.tpl", {
    cluster_name = aws_ecs_cluster.main.name
    nginx_conf   = local.nginx_conf
  })

  tags = {
    Name = var.project_name
  }
}

# Stable public IP - the domain's A record points here, and it survives
# instance replacement (e.g. a future AMI update), which a bare
# auto-assigned public IP would not.
resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"

  tags = {
    Name = var.project_name
  }
}
