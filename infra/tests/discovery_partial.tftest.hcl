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
