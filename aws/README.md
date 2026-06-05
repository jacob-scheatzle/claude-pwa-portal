# Deploying the portal on AWS (ECS Fargate + ALB + CloudFront)

This is an alternative to the Docker/Caddy-on-a-VPS deployment in
[../docs/deploying.md](../docs/deploying.md). It runs the **same portal image**
as a cloud-native stack:

```
Browser ──HTTPS──▶ CloudFront ──HTTPS──▶ ALB ──HTTP──▶ ECS Fargate task ──▶ RDS Postgres
        (ACM us-east-1,    (ACM regional,    (caddy :80 sidecar          └▶ S3 (bundles,
         WAF, edge TLS)     CloudFront-only    → uvicorn :8000)              storage, branding,
                            security group)                                  shares)

DNS:  portal.example.com  +  *.apps.example.com  ──▶ CloudFront
```

Same application, different backends: the database is **RDS PostgreSQL** instead
of SQLite, and all blob state (app bundles, per-user storage, branding, rendered
share PDFs) lives in **S3** instead of local disk. These are selected at runtime
by `STORAGE_BACKEND=s3` + `DATABASE_URL=postgresql+psycopg://…`; the local
SQLite/filesystem product is unchanged and still the default.

## What's different from the VPS deploy

| | VPS (docker compose) | AWS (this) |
|---|---|---|
| TLS | Caddy + Let's Encrypt | ACM on CloudFront + ALB |
| Per-app subdomain certs | Caddy on-demand TLS + `/cert-ask` | one ACM wildcard `*.apps.<domain>` |
| Database | SQLite on disk | RDS PostgreSQL |
| Blob storage | `./data` on disk | S3 bucket |
| Scanner/brute-force defense | fail2ban on the host | AWS WAF at the edge |
| Security logs | `data/security.log` | stdout → CloudWatch Logs |
| Backups | the in-app **Backup** button | RDS snapshots + S3 versioning |

The portal still runs as a **single task** (`desired_count = 1`) with
recreate-style deploys. Its scheduler and rate limiters are in-process and
require exactly one instance; moving the DB to Postgres does not change that.
True horizontal scaling is a separate, larger change.

## Prerequisites

- An AWS account and credentials configured locally (`aws sts get-caller-identity` works).
- A **Route53 hosted zone** for the domain you'll use (e.g. `example.com`). The
  Terraform creates the ACM validation records and the portal/`*.apps` aliases
  in it. (No Route53? See [Managing DNS yourself](#managing-dns-yourself).)
- `terraform` (>= 1.6), `docker` (with buildx), and the `aws` CLI on your machine.

## Deploy

```bash
cd aws/terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: set `domain` and `route53_zone_id`

cd ..
./scripts/bootstrap.sh
```

`bootstrap.sh` creates the ECR repo, builds + pushes the portal image
(`--build-arg INSTALL_AWS=true`, linux/amd64), then applies the full stack so the
ECS service comes up with the image already present. First apply takes a while —
RDS and the CloudFront distribution are the slow parts.

When it finishes, give the ECS service a minute to reach steady state and
CloudFront a few minutes to deploy, then open `https://<domain>/` and complete
the **first-run wizard** (creates the admin account). From here the app behaves
exactly like the VPS deploy: add users, configure SMTP under **Settings**,
upload apps, install on a phone.

## Updates

```bash
aws/scripts/deploy.sh
```

Builds + pushes a fresh image and forces a new ECS deployment (recreate — a brief
blip while the one task is replaced). Schema migrations run automatically when
the new task boots (`alembic upgrade head`), same as the VPS deploy.

## Backups

- **Database:** RDS automated backups are on (7-day retention). Take a manual
  snapshot before risky changes; restore via the RDS console.
- **Blobs:** the S3 bucket has **versioning enabled**, so overwritten/deleted
  objects are recoverable.

The in-app one-click **Backup** button (SQLite snapshot + tar of `./data`) is
**disabled on this deployment** — it returns a 400 pointing here, because there's
no local SQLite or filesystem to tar. The secret-free **Export** (Admin →
Export) still works and reads straight from S3.

## Cost (rough, us-east-1, low traffic)

ALB ~$16–22/mo · Fargate 0.5 vCPU/1 GB 24×7 ~$15/mo · RDS `db.t4g.micro` ~$12–15/mo
· S3 + ECR + Secrets + CloudWatch + WAF a few $/mo · CloudFront pennies at low
volume. **No NAT gateway** (the task egresses to AWS via VPC endpoints), which
avoids the usual ~$32/mo surprise. Ballpark **$50–70/mo**.

## Operational notes

- **Single task / recreate deploys.** `desired_count = 1`, `minimum_healthy=0 /
  maximum=100`: the old task stops before the new one starts, so two schedulers
  (or two writers) never overlap. Expect a short blip on each deploy.
- **The ALB is locked to CloudFront.** Its security group only admits the
  CloudFront origin-facing prefix list, and the listener only forwards requests
  carrying a secret `X-Origin-Verify` header CloudFront adds. Direct hits to the
  ALB get a 403 — this keeps the Host-based app routing and WAF from being
  bypassed.
- **Host header forwarding.** CloudFront uses the managed `AllViewer` origin
  request policy so the real `Host` reaches the portal — required for the
  `<slug>.apps.<domain>` dispatch. Caching is disabled (the app is dynamic and
  cookie-authed).
- **WAF may need tuning.** The CommonRuleSet `SizeRestrictions_BODY` rule is set
  to *count* (not block) because legitimate paths take large bodies (app `.zip`
  uploads up to 50 MiB, PDF-render HTML). If you see false positives on other
  rules, add `rule_action_override` blocks in `waf.tf`.
- **Outbound email (SMTP).** There's no NAT, so the task can't reach an arbitrary
  SMTP host on the internet. Options: use **SES** via its VPC endpoint, or add a
  NAT gateway + a default route on the private route table (`network.tf`).
- **`SECRET_KEY` is generated once** and stored in Secrets Manager. Don't rotate
  it casually — it invalidates every session cookie and breaks the
  Fernet-encrypted SMTP password at rest.
- **Logs** are in CloudWatch under `/ecs/<project>` (streams `portal` and
  `caddy`); security events (failed logins, etc.) are emitted to stdout there too.

## Managing DNS yourself

If the domain isn't in Route53, remove `route53.tf` and the
`aws_route53_record.validation` resources in `acm.tf`, run `terraform apply` to
get the ACM validation CNAMEs from the certificate resources, create them in your
DNS provider by hand, then point `domain` and `*.apps.<domain>` (CNAME/ALIAS) at
the `cloudfront_domain` output. Everything else is unchanged.

## Teardown

```bash
cd aws/terraform
# empty the data bucket first (versioned buckets won't delete while non-empty)
aws s3 rm "s3://$(terraform output -raw s3_bucket)" --recursive
terraform destroy
```

RDS `skip_final_snapshot = true` and `deletion_protection = false` are set for
easy teardown — flip those in `rds.tf` for a long-lived production database.
