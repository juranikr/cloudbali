resource "aws_cloudwatch_log_group" "discovery" {
  name              = "/ecs/cloudbali-prod-discovery"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "discovery_workflow" {
  name              = "/aws/vendedlogs/states/cloudbali-prod-discovery"
  retention_in_days = 14
}

resource "aws_ecs_task_definition" "discovery" {
  family                   = "cloudbali-prod-discovery"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "discovery"
    image     = format("%s:latest", aws_ecr_repository.app.repository_url)
    essential = true
    command   = var.discovery_worker_command
    environment = [
      { name = "APP_NAME", value = var.app_name },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "SEED_TEST_ACCOUNT", value = "false" },
      { name = "DISCOVERY_RUN_ID", value = "0" },
      { name = "DISCOVERY_REGION_ID", value = "0" },
      { name = "DISCOVERY_LIMIT", value = "60" },
      { name = "DISCOVERY_RETRY_COUNT", value = "0" },
      { name = "DISCOVERY_TRIGGER", value = "schedule" },
      { name = "DISCOVERY_WORKER_MODE", value = var.discovery_worker_mode },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = format("%s:DATABASE_URL::", data.aws_secretsmanager_secret.app.arn) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.discovery.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "discovery"
      }
    }
  }])

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role" "discovery_workflow" {
  name = "cloudbali-prod-discovery-sfn"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "discovery_workflow" {
  name = "cloudbali-prod-discovery-sfn"
  role = aws_iam_role.discovery_workflow.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunDiscoveryTask"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [aws_ecs_task_definition.discovery.arn]
      },
      {
        Sid      = "PassDiscoveryRoles"
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

resource "aws_sfn_state_machine" "discovery" {
  name     = "cloudbali-prod-discovery"
  role_arn = aws_iam_role.discovery_workflow.arn
  type     = "STANDARD"

  logging_configuration {
    include_execution_data = true
    level                  = "ALL"
    log_destination        = "${aws_cloudwatch_log_group.discovery_workflow.arn}:*"
  }

  definition = jsonencode({
    Comment = "Run one durable, review-only Bali place discovery worker"
    StartAt = "RunDiscoveryWorker"
    States = {
      RunDiscoveryWorker = {
        Type           = "Task"
        Resource       = "arn:aws:states:::ecs:runTask.waitForTaskToken"
        TimeoutSeconds = 1800
        Parameters = {
          Cluster         = data.aws_ecs_cluster.shared.arn
          TaskDefinition  = aws_ecs_task_definition.discovery.arn
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
              Name = "discovery"
              Environment = [
                { Name = "SFN_TASK_TOKEN", "Value.$" = "$$.Task.Token" },
                { Name = "DISCOVERY_RUN_ID", "Value.$" = "States.Format('{}', $.run_id)" },
                { Name = "DISCOVERY_REGION_ID", "Value.$" = "States.Format('{}', $.region_id)" },
                { Name = "DISCOVERY_LIMIT", "Value.$" = "States.Format('{}', $.limit)" },
                { Name = "DISCOVERY_RETRY_COUNT", "Value.$" = "States.Format('{}', $$.State.RetryCount)" },
                { Name = "DISCOVERY_TRIGGER", "Value.$" = "$.trigger" },
                { Name = "DISCOVERY_WORKER_MODE", "Value.$" = "$.worker_mode" },
              ]
            }]
          }
        }
        Retry = [{
          ErrorEquals = [
            "DiscoveryRunFailed",
            "DiscoveryWorkerFailed",
            "ECS.AmazonECSException",
            "ECS.ThrottlingException",
            "States.TaskFailed",
            "States.Timeout",
          ]
          IntervalSeconds = 30
          BackoffRate     = 2
          MaxAttempts     = 3
        }]
        ResultPath = "$.task_result"
        End        = true
      }
    }
  })

  depends_on = [aws_iam_role_policy.discovery_workflow]
}

resource "aws_iam_role_policy" "task_discovery_workflow" {
  name = "cloudbali-prod-discovery-dispatch"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StartDiscovery"
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = [aws_sfn_state_machine.discovery.arn]
      },
      {
        Sid      = "DescribeDiscoveryExecution"
        Effect   = "Allow"
        Action   = ["states:DescribeExecution"]
        Resource = ["${replace(aws_sfn_state_machine.discovery.arn, ":stateMachine:", ":execution:")}:*"]
      },
      {
        Sid    = "ReportDiscoveryTaskOutcome"
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

resource "aws_sqs_queue" "discovery_dlq" {
  name                      = "cloudbali-prod-discovery-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

data "aws_iam_policy_document" "discovery_dlq" {
  statement {
    sid     = "AllowEventBridgeDeliveryFailure"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]
    resources = [
      aws_sqs_queue.discovery_dlq.arn,
    ]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.discovery.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "discovery_dlq" {
  queue_url = aws_sqs_queue.discovery_dlq.id
  policy    = data.aws_iam_policy_document.discovery_dlq.json
}

resource "aws_sns_topic" "operations_alerts" {
  name = "cloudbali-prod-operations-alerts"
}

resource "aws_sns_topic_subscription" "operations_email" {
  count = trimspace(var.operations_alert_email) == "" ? 0 : 1

  topic_arn = aws_sns_topic.operations_alerts.arn
  protocol  = "email"
  endpoint  = trimspace(var.operations_alert_email)
}

resource "aws_cloudwatch_metric_alarm" "discovery_failed" {
  alarm_name          = "cloudbali-prod-discovery-failed"
  alarm_description   = "The durable place-discovery workflow failed after retries"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.discovery.arn
  }

  alarm_actions = [aws_sns_topic.operations_alerts.arn]
  ok_actions    = [aws_sns_topic.operations_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "discovery_timed_out" {
  alarm_name          = "cloudbali-prod-discovery-timed-out"
  alarm_description   = "The durable place-discovery workflow timed out"
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.discovery.arn
  }

  alarm_actions = [aws_sns_topic.operations_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "discovery_dlq_visible" {
  alarm_name          = "cloudbali-prod-discovery-dlq-visible"
  alarm_description   = "EventBridge could not deliver a scheduled discovery execution"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.discovery_dlq.name
  }

  alarm_actions = [aws_sns_topic.operations_alerts.arn]
  ok_actions    = [aws_sns_topic.operations_alerts.arn]
}
