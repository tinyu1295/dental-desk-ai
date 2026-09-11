variable "aws_region" {
  description = "AWS region to deploy into. Must match the region baked into services/voice_gateway/app/bedrock_stream.py's BedrockStreamManager(region=...) default (us-east-1) unless that's changed too."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used as a prefix for resource names"
  type        = string
  default     = "dental-desk-ai"
}

variable "container_port" {
  description = "Port both FastAPI apps listen on inside their containers"
  type        = number
  default     = 8000
}

variable "booking_service_task_cpu" {
  description = "Task-level CPU units, soft limit (256 = 0.25 vCPU) - ECS EC2 launch type shares the host's CPU rather than reserving a Fargate slice"
  type        = number
  default     = 128
}

variable "booking_service_task_memory_reservation" {
  description = "Soft memory limit (MB) for booking_service - the lighter of the two containers (no audio processing), so it gets a smaller share of the host's RAM than voice_gateway"
  type        = number
  default     = 256
}

variable "voice_gateway_task_cpu" {
  description = "Task-level CPU units, soft limit - same reasoning as booking_service_task_cpu"
  type        = number
  default     = 128
}

variable "voice_gateway_task_memory_reservation" {
  description = "Soft memory limit (MB) for voice_gateway - audio resampling and the live Bedrock stream make this the heavier of the two containers"
  type        = number
  default     = 512
}

variable "ec2_instance_type" {
  description = "Instance type for the single EC2 host running the ECS agent + both containers. t2.small, not t3/t4g.small - a new AWS account's EC2 'Standard' On-Demand vCPU quota is commonly capped at 1, and t3/t4g both need 2 vCPUs where t2 needs only 1 (same reasoning as the hotel-voice-agent project this was adapted from). Bumped to .small (2GB) rather than .micro (1GB) because this host now runs two application containers instead of one, on top of the ECS agent, nginx, and certbot."
  type        = string
  default     = "t2.small"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro is one of the cheapest Postgres-capable classes and is free-tier eligible on a new account for 12 months; RDS has its own quota family separate from EC2's, so the t2/t3/t4g vCPU constraint above doesn't apply here."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "RDS storage in GB - 20 is the practical minimum for gp3 Postgres on RDS"
  type        = number
  default     = 20
}

variable "db_backup_retention_days" {
  description = "Automated RDS backup retention. Kept short and cheap for a demo project rather than 0 (no backups at all)."
  type        = number
  default     = 1
}

variable "db_name" {
  description = "Database name - matches POSTGRES_DB in .env.example / docker-compose.yml"
  type        = string
  default     = "dental"
}

variable "db_username" {
  description = "Master username - matches POSTGRES_USER in .env.example / docker-compose.yml"
  type        = string
  default     = "dental"
}

variable "domain_name" {
  description = "Domain name pointed at the EC2 instance's Elastic IP (Route53), used for the Let's Encrypt cert and Twilio's webhook / PUBLIC_BASE_URL / MEDIA_STREAM_URL"
  type        = string
}

variable "admin_email" {
  description = "Email passed to certbot for Let's Encrypt renewal/expiry notices"
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention period"
  type        = number
  default     = 14
}

variable "twilio_auth_token" {
  description = "Twilio Account Auth Token, used to validate inbound webhook signatures. Supply via terraform.tfvars (gitignored) or TF_VAR_twilio_auth_token."
  type        = string
  sensitive   = true
}
