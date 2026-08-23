resource "aws_cloudwatch_log_group" "batch" {
  name              = "/ecs/cloudbali-prod-batch"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "batch" {
  family                   = "cloudbali-prod-batch"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name      = "batch"
    image     = format("%s:latest", aws_ecr_repository.app.repository_url)
    essential = true
    command   = ["python", "-m", "app.batch"]
    environment = [
      { name = "APP_NAME", value = var.app_name },
      { name = "SEED_TEST_ACCOUNT", value = "false" },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = format("%s:DATABASE_URL::", data.aws_secretsmanager_secret.app.arn) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.batch.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "batch"
      }
    }
  }])

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role" "events" {
  name = "cloudbali-prod-events"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "events" {
  name = "cloudbali-run-batch"
  role = aws_iam_role.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.batch.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.discovery.arn]
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "batch" {
  name                = "cloudbali-prod-batch-6h"
  description         = "Refresh Bali traveler conditions every six hours"
  schedule_expression = "rate(6 hours)"
}

resource "aws_cloudwatch_event_target" "batch" {
  rule      = aws_cloudwatch_event_rule.batch.name
  target_id = "cloudbali-batch"
  arn       = data.aws_ecs_cluster.shared.arn
  role_arn  = aws_iam_role.events.arn
  input     = jsonencode({})

  ecs_target {
    task_count          = 1
    task_definition_arn = aws_ecs_task_definition.batch.arn
    launch_type         = "FARGATE"
    platform_version    = "LATEST"

    network_configuration {
      subnets          = var.public_subnet_ids
      security_groups  = [aws_security_group.ecs.id]
      assign_public_ip = true
    }
  }

  depends_on = [aws_iam_role_policy.events]
}

# Bali time 03:30 (UTC 19:30). Discovery only creates review candidates;
# it never publishes a place without an administrator approval.
resource "aws_cloudwatch_event_rule" "discovery" {
  name                = "cloudbali-prod-discovery-daily"
  description         = "Discover reviewable Bali-area place candidates once a day"
  schedule_expression = "cron(30 19 * * ? *)"
}

resource "aws_cloudwatch_event_target" "discovery" {
  rule      = aws_cloudwatch_event_rule.discovery.name
  target_id = "cloudbali-place-discovery"
  arn       = aws_sfn_state_machine.discovery.arn
  role_arn  = aws_iam_role.events.arn
  input = jsonencode({
    run_id      = 0
    region_id   = 0
    limit       = 60
    trigger     = "schedule"
    worker_mode = var.discovery_worker_mode
  })

  dead_letter_config {
    arn = aws_sqs_queue.discovery_dlq.arn
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 3
  }

  depends_on = [aws_iam_role_policy.events, aws_sqs_queue_policy.discovery_dlq]
}
