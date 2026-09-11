# SecureString, Standard tier - free for parameters this small. The AWS
# key pair is fed directly from aws_iam_access_key.app's own attributes
# and the DB password from random_password.db's, never manually
# copy-pasted anywhere.

resource "aws_ssm_parameter" "aws_access_key_id" {
  name  = "/${var.project_name}/aws-access-key-id"
  type  = "SecureString"
  value = aws_iam_access_key.app.id

  tags = {
    Name = "${var.project_name}-aws-access-key-id"
  }
}

resource "aws_ssm_parameter" "aws_secret_access_key" {
  name  = "/${var.project_name}/aws-secret-access-key"
  type  = "SecureString"
  value = aws_iam_access_key.app.secret

  tags = {
    Name = "${var.project_name}-aws-secret-access-key"
  }
}

resource "aws_ssm_parameter" "twilio_auth_token" {
  name  = "/${var.project_name}/twilio-auth-token"
  type  = "SecureString"
  value = var.twilio_auth_token

  tags = {
    Name = "${var.project_name}-twilio-auth-token"
  }
}

resource "aws_ssm_parameter" "postgres_password" {
  name  = "/${var.project_name}/postgres-password"
  type  = "SecureString"
  value = random_password.db.result

  tags = {
    Name = "${var.project_name}-postgres-password"
  }
}
