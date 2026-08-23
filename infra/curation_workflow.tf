resource "aws_cloudwatch_log_group" "curation" {
  name              = "/ecs/cloudbali-prod-curation"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "curation_workflow" {
  name              = "/aws/vendedlogs/states/cloudbali-prod-curation"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "curation" {
  family                   = "cloudbali-prod-curation"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "curation"
    image     = format("%s:latest", aws_ecr_repository.app.repository_url)
    essential = true
    command   = var.curation_worker_command
    environment = [
      { name = "APP_NAME", value = var.app_name },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "SEED_TEST_ACCOUNT", value = "false" },
      { name = "CURATION_RUN_ID", value = "0" },
      { name = "CURATION_RETRY_COUNT", value = "0" },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = format("%s:DATABASE_URL::", data.aws_secretsmanager_secret.app.arn) },
      { name = "GROQ_API_KEY", valueFrom = format("%s:GROQ_API_KEY::", data.aws_secretsmanager_secret.app.arn) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.curation.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "curation"
      }
    }
  }])

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role" "curation_workflow" {
  name = "cloudbali-prod-curation-sfn"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "curation_workflow" {
  name = "cloudbali-prod-curation-sfn"
  role = aws_iam_role.curation_workflow.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunCurationTask"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.curation.arn]
      },
      {
        Sid      = "PassCurationRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
      },
      {
        Sid    = "ManageWorkflowLogDelivery"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery",
          "logs:DeleteLogDelivery",
          "logs:DescribeLogGroups",
          "logs:DescribeResourcePolicies",
          "logs:GetLogDelivery",
          "logs:ListLogDeliveries",
          "logs:PutResourcePolicy",
          "logs:UpdateLogDelivery",
        ]
        Resource = ["*"]
      },
    ]
  })
}

resource "aws_sfn_state_machine" "curation" {
  name     = "cloudbali-prod-curation"
  role_arn = aws_iam_role.curation_workflow.arn
  type     = "STANDARD"

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"
    log_destination        = "${aws_cloudwatch_log_group.curation_workflow.arn}:*"
  }

  definition = jsonencode({
    Comment = "Run one auditable, proposal-only Bali multi-source curation worker"
    StartAt = "RunCurationWorker"
    States = {
      RunCurationWorker = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.waitForTaskToken"
        TimeoutSeconds = 2700
        Parameters = {
          Cluster         = data.aws_ecs_cluster.shared.arn
          TaskDefinition  = aws_ecs_task_definition.curation.arn
          LaunchType      = "FARGATE"
          PlatformVersion = "LATEST"
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = var.public_subnet_ids
              SecurityGroups = [aws_security_group.ecs.id]
              AssignPublicIp = "ENABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [{
              Name = "curation"
              Environment = [
                { Name = "SFN_TASK_TOKEN", "Value.$" = "$$.Task.Token" },
                { Name = "CURATION_RUN_ID", "Value.$" = "States.Format('{}', $.run_id)" },
                { Name = "CURATION_RETRY_COUNT", "Value.$" = "States.Format('{}', $$.State.RetryCount)" },
              ]
            }]
          }
        }
        Retry = [{
          ErrorEquals = [
            "CurationRunFailed",
            "CurationWorkerFailed",
            "ECS.AmazonECSException",
            "ECS.ThrottlingException",
            "States.TaskFailed",
            "States.Timeout",
          ]
          IntervalSeconds = 45
          BackoffRate     = 2
          MaxAttempts     = 2
        }]
        ResultPath = "$.task_result"
        End        = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.curation_workflow]
}

resource "aws_iam_role_policy" "task_curation_workflow" {
  name = "cloudbali-prod-curation-dispatch"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartCuration"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.curation.arn]
      },
      {
        Sid      = "DescribeCurationExecution"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution"]
        Resource = ["${replace(aws_sfn_state_machine.curation.arn, ":stateMachine:", ":execution:")}:*"]
      },
      {
        Sid    = "ReportCurationTaskOutcome"
        Effect = "Allow"
        Action = [
          "states:SendTaskFailure",
          "states:SendTaskSuccess",
        ]
        Resource = ["*"]
      },
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "curation_failed" {
  alarm_name          = "cloudbali-prod-curation-failed"
  alarm_description   = "The multi-source curation workflow failed after retries"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.curation.arn
  }

  alarm_actions = [aws_sns_topic.operations_alerts.arn]
  ok_actions    = [aws_sns_topic.operations_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "curation_timed_out" {
  alarm_name          = "cloudbali-prod-curation-timed-out"
  alarm_description   = "The multi-source curation workflow timed out"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.curation.arn
  }

  alarm_actions = [aws_sns_topic.operations_alerts.arn]
}
