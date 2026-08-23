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

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}
