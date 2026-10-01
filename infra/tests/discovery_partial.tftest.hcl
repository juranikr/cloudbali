mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/terraform-test"
      user_id    = "AIDATEST"
    }
  }

  mock_data "aws_ecs_cluster" {
    defaults = {
      arn = "arn:aws:ecs:ap-northeast-2:123456789012:cluster/terraform-test"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_secretsmanager_secret" {
    defaults = {
      arn = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:terraform-test"
    }
  }

  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:ap-northeast-2:123456789012:cloudbali-prod-operations-alerts"
    }
  }
}

mock_provider "random" {}

run "discovery_partial_alert_contract" {
  command = plan

  assert {
    condition     = aws_cloudwatch_log_metric_filter.discovery_partial.log_group_name == aws_cloudwatch_log_group.discovery.name
    error_message = "The partial metric filter must read the discovery worker log group."
  }

  assert {
    condition     = aws_cloudwatch_log_metric_filter.discovery_partial.pattern == "{ $.status = \"partial\" }"
    error_message = "The partial metric filter must match the worker's exact JSON status field."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.discovery_partial.namespace == "CloudBali/Discovery" && aws_cloudwatch_metric_alarm.discovery_partial.metric_name == "PartialExecutions"
    error_message = "The partial alarm must use its dedicated custom metric."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.discovery_partial.threshold == 1 && aws_cloudwatch_metric_alarm.discovery_partial.comparison_operator == "GreaterThanOrEqualToThreshold"
    error_message = "One partial discovery execution must trigger the alarm."
  }

  assert {
    condition     = length(aws_cloudwatch_metric_alarm.discovery_partial.alarm_actions) == 1
    error_message = "The partial alarm must keep exactly one operations notification action."
  }
}

run "hibernation_contract" {
  command = plan

  variables {
    api_enabled            = false
    scheduled_jobs_enabled = false
    desired_count          = 1
  }

  assert {
    condition     = aws_ecs_service.app.desired_count == 0
    error_message = "Hibernation must scale the API service to zero tasks."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.batch.state == "DISABLED"
    error_message = "Hibernation must disable the six-hour conditions batch."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.discovery.state == "DISABLED"
    error_message = "Hibernation must disable the daily discovery schedule."
  }
}

run "active_runtime_contract" {
  command = plan

  variables {
    api_enabled            = true
    scheduled_jobs_enabled = true
    desired_count          = 1
  }

  assert {
    condition     = aws_ecs_service.app.desired_count == 1
    error_message = "Reactivation must restore the configured API task count."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.batch.state == "ENABLED" && aws_cloudwatch_event_rule.discovery.state == "ENABLED"
    error_message = "Reactivation must restore both scheduled jobs."
  }
}
