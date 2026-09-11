# One log group per service, not one shared group - keeps
# `aws logs tail` scoped to one component instead of interleaving both
# services' output, and lets retention be tuned per service later if
# they ever need to differ.

resource "aws_cloudwatch_log_group" "booking_service" {
  name              = "/ecs/${var.project_name}/booking-service"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-booking-service-logs"
  }
}

resource "aws_cloudwatch_log_group" "voice_gateway" {
  name              = "/ecs/${var.project_name}/voice-gateway"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-voice-gateway-logs"
  }
}
