output "app_url" {
  value = format("https://%s", aws_cloudfront_distribution.app.domain_name)
}
output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.app.id
}
output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
output "ecs_cluster_name" {
  value = data.aws_ecs_cluster.shared.cluster_name
}
output "ecs_service_name" {
  value = aws_ecs_service.app.name
}
output "runtime_secret_arn" {
  value = data.aws_secretsmanager_secret.app.arn
}

output "batch_task_definition_arn" {
  value = aws_ecs_task_definition.batch.arn
}

output "discovery_task_definition_arn" {
  value = aws_ecs_task_definition.discovery.arn
}

output "discovery_state_machine_arn" {
  value = aws_sfn_state_machine.discovery.arn
}

output "curation_task_definition_arn" {
  value = aws_ecs_task_definition.curation.arn
}

output "curation_state_machine_arn" {
  value = aws_sfn_state_machine.curation.arn
}

output "discovery_dead_letter_queue_url" {
  value = aws_sqs_queue.discovery_dlq.url
}

output "operations_alert_topic_arn" {
  value = aws_sns_topic.operations_alerts.arn
}

output "archive_bucket" {
  description = "Private versioned bucket for hibernation database and code recovery artifacts."
  value       = aws_s3_bucket.archive.bucket
}

output "place_image_bucket" {
  value = aws_s3_bucket.place_images.bucket
}

output "place_image_public_base_url" {
  value = format("https://%s", aws_cloudfront_distribution.place_images.domain_name)
}

output "place_image_cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.place_images.id
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}
