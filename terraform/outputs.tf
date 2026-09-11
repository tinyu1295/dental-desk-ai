output "booking_service_ecr_repository_url" {
  description = "ECR repository URL to push the booking_service image to"
  value       = aws_ecr_repository.booking_service.repository_url
}

output "voice_gateway_ecr_repository_url" {
  description = "ECR repository URL to push the voice_gateway image to"
  value       = aws_ecr_repository.voice_gateway.repository_url
}

output "booking_service_log_group_name" {
  value = aws_cloudwatch_log_group.booking_service.name
}

output "voice_gateway_log_group_name" {
  value = aws_cloudwatch_log_group.voice_gateway.name
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_ids" {
  value = [for s in aws_subnet.public : s.id]
}

output "private_subnet_ids" {
  value = [for s in aws_subnet.private : s.id]
}

output "db_endpoint" {
  description = "RDS endpoint (host:port) - not directly reachable outside the VPC (publicly_accessible = false)"
  value       = aws_db_instance.app.endpoint
}

output "ec2_public_ip" {
  description = "Stable Elastic IP of the EC2 instance - point the domain's A record here"
  value       = aws_eip.app.public_ip
}

output "ec2_instance_id" {
  description = "For `aws ssm start-session --target <id>` (no SSH key/port 22 needed)"
  value       = aws_instance.app.id
}

output "public_url" {
  description = "Public HTTPS/WSS entry point - this is what Twilio's webhook config should point at"
  value       = "https://${var.domain_name}"
}
