data "aws_ecs_cluster" "shared" {
  cluster_name = var.ecs_cluster_name
}

data "aws_secretsmanager_secret" "app" {
  name = "cloudbali-prod/app"
}

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "random_password" "origin_header" {
  length  = 32
  special = false
}

resource "aws_ecr_repository" "app" {
  name                 = "cloudbali-prod-api"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the ten newest images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/cloudbali-prod-api"
  retention_in_days = 14
}

resource "aws_iam_role" "execution" {
  name = "cloudbali-prod-ecs-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "secret" {
  name = "cloudbali-runtime-secret"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.app.arn
    }]
  })
}

resource "aws_iam_role" "task" {
  name = "cloudbali-prod-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_security_group" "ecs" {
  name        = "cloudbali-prod-ecs-sg"
  description = "cloudbali ECS tasks"
  vpc_id      = var.vpc_id
  ingress {
    description     = "App traffic from the shared ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [var.alb_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_cloudbali" {
  security_group_id            = var.rds_security_group_id
  referenced_security_group_id = aws_security_group.ecs.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Postgres from cloudbali ECS"
}

resource "aws_lb_target_group" "app" {
  name        = "cloudbali-prod-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id
  health_check {
    enabled             = true
    path                = "/api/health"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
    matcher             = "200"
  }
  deregistration_delay = 20
}

resource "aws_lb_listener_rule" "cloudbali" {
  listener_arn = var.alb_listener_arn
  priority     = 200
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
  condition {
    http_header {
      http_header_name = "X-Cloudbali-Origin"
      values           = [random_password.origin_header.result]
    }
  }
}

resource "aws_cloudfront_distribution" "app" {
  enabled             = true
  is_ipv6_enabled     = true
  comment             = "cloudbali-prod https frontend+api"
  price_class         = "PriceClass_200"
  http_version        = "http2and3"
  wait_for_deployment = true
  origin {
    domain_name = var.alb_dns_name
    origin_id   = "shared-alb"
    custom_header {
      name  = "X-Cloudbali-Origin"
      value = random_password.origin_header.result
    }
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
      origin_read_timeout    = 30
    }
  }
  default_cache_behavior {
    target_origin_id         = "shared-alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD", "OPTIONS"]
    compress                 = true
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_except_host.id
  }
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

resource "aws_ecs_task_definition" "app" {
  family                   = "cloudbali-prod-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name      = "api"
    image     = format("%s:latest", aws_ecr_repository.app.repository_url)
    essential = true
    portMappings = [{
      containerPort = 8000
      hostPort      = 8000
      protocol      = "tcp"
    }]
    environment = [
      { name = "APP_NAME", value = var.app_name },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "ADMIN_EMAILS", value = "joohan92@naver.com,tjwjd629@naver.com" },
      { name = "SEED_TEST_ACCOUNT", value = "false" },
      { name = "CORS_ORIGINS", value = format("https://%s", aws_cloudfront_distribution.app.domain_name) },
      { name = "GEOCODER_USER_AGENT", value = "cloudbali-production/1.0" },
      { name = "S3_BUCKET", value = aws_s3_bucket.place_images.bucket },
      { name = "S3_PUBLIC_BASE_URL", value = format("https://%s", aws_cloudfront_distribution.place_images.domain_name) },
      { name = "DISCOVERY_STATE_MACHINE_ARN", value = aws_sfn_state_machine.discovery.arn },
      { name = "DISCOVERY_WORKER_MODE", value = var.discovery_worker_mode },
      { name = "CURATION_STATE_MACHINE_ARN", value = aws_sfn_state_machine.curation.arn },
    ]
    secrets = [
      { name = "DATABASE_URL", valueFrom = format("%s:DATABASE_URL::", data.aws_secretsmanager_secret.app.arn) },
      { name = "JWT_SECRET", valueFrom = format("%s:JWT_SECRET::", data.aws_secretsmanager_secret.app.arn) },
      { name = "SEED_PASSWORD_JOOHAN", valueFrom = format("%s:SEED_PASSWORD_JOOHAN::", data.aws_secretsmanager_secret.app.arn) },
      { name = "SEED_PASSWORD_GUKSEO", valueFrom = format("%s:SEED_PASSWORD_GUKSEO::", data.aws_secretsmanager_secret.app.arn) },
      { name = "GROQ_API_KEY", valueFrom = format("%s:GROQ_API_KEY::", data.aws_secretsmanager_secret.app.arn) },
      { name = "GROQ_CHAT_MODEL", valueFrom = format("%s:GROQ_CHAT_MODEL::", data.aws_secretsmanager_secret.app.arn) },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')\" || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
  }])

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_ecs_service" "app" {
  name            = "cloudbali-prod-api"
  cluster         = data.aws_ecs_cluster.shared.arn
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "api"
    container_port   = 8000
  }
  depends_on = [
    aws_lb_listener_rule.cloudbali,
    aws_iam_role_policy.secret,
  ]
}
