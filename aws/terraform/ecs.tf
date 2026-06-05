resource "aws_security_group" "task" {
  name_prefix = "${local.name}-task-"
  description = "Fargate task: ingress from the ALB only"
  vpc_id      = aws_vpc.main.id
  ingress {
    description     = "Caddy sidecar from the ALB"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${local.name}-task-sg" }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# Two containers sharing one task ENI (awsvpc): the Caddy sidecar terminates the
# ALB's plain HTTP on :80 and reverse-proxies to the portal on 127.0.0.1:8000.
# Caddy reuses the existing Caddyfile.http (apex + *.apps wildcard + the
# security headers); PORTAL_UPSTREAM points it at localhost instead of the
# compose "portal" hostname.
resource "aws_ecs_task_definition" "portal" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name         = "portal"
      image        = "${aws_ecr_repository.portal.repository_url}:${var.image_tag}"
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment = [
        { name = "SITE_URL", value = var.domain },
        { name = "STORAGE_BACKEND", value = "s3" },
        { name = "S3_BUCKET", value = aws_s3_bucket.data.id },
        { name = "S3_REGION", value = var.region },
        { name = "AWS_REGION", value = var.region },
        { name = "COOKIES_SECURE", value = "true" },
        { name = "CHILD_APPS_SAME_ORIGIN", value = "false" },
        { name = "MCP_ENABLED", value = var.mcp_enabled },
      ]
      secrets = [
        { name = "SECRET_KEY", valueFrom = aws_secretsmanager_secret.secret_key.arn },
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.portal.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "portal"
        }
      }
    },
    {
      name         = "caddy"
      image        = var.caddy_image
      essential    = true
      portMappings = [{ containerPort = 80, protocol = "tcp" }]
      dependsOn    = [{ containerName = "portal", condition = "START" }]
      environment = [
        { name = "SITE_URL", value = var.domain },
        { name = "HTTP_ONLY", value = "true" },
        { name = "PORTAL_UPSTREAM", value = "127.0.0.1:8000" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.portal.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "caddy"
        }
      }
    },
  ])
}

resource "aws_ecs_service" "portal" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.portal.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  # Single instance, recreate-style deploys. The in-process scheduler and the
  # login/PDF/email rate limiters require exactly one task; moving the DB to
  # Postgres doesn't make the scheduler multi-safe. min 0 / max 100 stops the
  # old task before starting the new one — a brief deploy blip, but never two
  # schedulers (or two SQLite-style writers) overlapping.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.portal.arn
    container_name   = "caddy"
    container_port   = 80
  }

  depends_on = [aws_lb_listener.https]
}
