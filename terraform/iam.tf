data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- ECS execution role: pulls both images, writes both services' logs,
# resolves the SSM secrets referenced in either task definition's
# `secrets` block. One shared role for both task definitions - nothing
# about it is task-specific. ---
resource "aws_iam_role" "ecs_execution" {
  name               = "${var.project_name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_ssm" {
  statement {
    effect  = "Allow"
    actions = ["ssm:GetParameters"]
    resources = [
      aws_ssm_parameter.aws_access_key_id.arn,
      aws_ssm_parameter.aws_secret_access_key.arn,
      aws_ssm_parameter.twilio_auth_token.arn,
      aws_ssm_parameter.postgres_password.arn,
    ]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_role_policy" "ecs_execution_ssm" {
  name   = "${var.project_name}-ecs-execution-ssm"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_ssm.json
}

# No aws_iam_role for task_role_arn - unlike a setup where the app calls
# AWS APIs via the task's own IAM role, this app authenticates its own
# AWS calls with the dedicated IAM user's static keys below, injected as
# env vars (see bedrock_stream.py's EnvironmentCredentialsResolver()).
# A task role would sit there unused, so it's left out rather than added
# defensively.

# --- Dedicated IAM user: its static access key is what actually
# authenticates voice_gateway's calls to Bedrock at runtime (see
# EnvironmentCredentialsResolver() in bedrock_stream.py, which only reads
# from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars - never the
# instance or task role). Scoped to exactly the one model this app
# calls, nothing broader. ---
resource "aws_iam_user" "app" {
  name = "${var.project_name}-app"
}

resource "aws_iam_access_key" "app" {
  user = aws_iam_user.app.name
}

data "aws_iam_policy_document" "app_user_permissions" {
  statement {
    effect = "Allow"
    actions = [
      "bedrock:InvokeModelWithBidirectionalStream",
      "bedrock:InvokeModel",
    ]
    # Must match BedrockStreamManager's model_id default
    # ("amazon.nova-2-sonic-v1:0") in bedrock_stream.py exactly.
    resources = [
      "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.nova-2-sonic-v1:0",
    ]
  }
}

resource "aws_iam_user_policy" "app" {
  name   = "${var.project_name}-app-permissions"
  user   = aws_iam_user.app.name
  policy = data.aws_iam_policy_document.app_user_permissions.json
}
