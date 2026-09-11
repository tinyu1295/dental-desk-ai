# SSM-parameter-sourced, not hardcoded - same reasoning as ec2.tf's AMI
# lookup: pin to a major version, let AWS resolve the current supported
# minor, so this doesn't go stale as RDS deprecates old minors over time.
data "aws_rds_engine_version" "postgres" {
  engine  = "postgres"
  version = "16"
  latest  = true
}

resource "random_password" "db" {
  length  = 32
  special = false # simplest to pass safely through an ECS task's env var either way
}

resource "aws_db_subnet_group" "app" {
  name       = "${var.project_name}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project_name}-db"
  }
}

# Ingress only from the EC2 host's own security group - nothing else in
# or outside the VPC can reach Postgres directly. booking_service is the
# only thing that ever talks to this database.
resource "aws_security_group" "rds" {
  name        = "${var.project_name}-rds"
  description = "Postgres access from the app host only"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "Postgres from the EC2 host"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.ec2.id]
  }

  tags = {
    Name = "${var.project_name}-rds-sg"
  }
}

resource "aws_db_instance" "app" {
  identifier     = "${var.project_name}-db"
  engine         = "postgres"
  engine_version = data.aws_rds_engine_version.postgres.version

  instance_class    = var.db_instance_class
  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.app.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  backup_retention_period = var.db_backup_retention_days
  # False so `terraform destroy` can actually tear this down for a demo
  # project - a real production database would flip this to true.
  deletion_protection = false
  skip_final_snapshot = true

  tags = {
    Name = "${var.project_name}-db"
  }
}
