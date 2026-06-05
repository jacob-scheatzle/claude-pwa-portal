# SECRET_KEY: generated once and kept stable. Rotating it invalidates every
# signed cookie (logs everyone out) and breaks the Fernet-encrypted SMTP
# password at rest, so it lives here and is never auto-rotated.
resource "random_password" "secret_key" {
  length  = 48
  special = false
}

resource "random_password" "db" {
  length  = 32
  special = false # keep it URL-safe for the DATABASE_URL below
}

resource "aws_secretsmanager_secret" "secret_key" {
  name = "${local.name}/secret-key"
}

resource "aws_secretsmanager_secret_version" "secret_key" {
  secret_id     = aws_secretsmanager_secret.secret_key.id
  secret_string = random_password.secret_key.result
}

# Full SQLAlchemy URL assembled from the RDS instance + generated password, so
# the task can inject one DATABASE_URL secret.
resource "aws_secretsmanager_secret" "database_url" {
  name = "${local.name}/database-url"
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg://${aws_db_instance.main.username}:${urlencode(random_password.db.result)}@${aws_db_instance.main.address}:5432/${aws_db_instance.main.db_name}"
}
