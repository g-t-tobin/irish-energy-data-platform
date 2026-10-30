# Administrator handover

Date: 2026-10-30. Audience: an AWS administrator who will build and deploy Irish
Energy Analytics. Nothing described here has been deployed or created. Commands are
for the administrator to run after review; none was run while preparing this document.

Read with `docs/production-readiness-review.md` (findings and blockers),
`docs/deployment-dns-and-certificates.md` (certificates and DNS),
`docs/adr/0001-shared-run-history-and-step-functions.md` and
`docs/security-and-production-architecture.md`.

Labels: **[Confirmed]** seen in this repository or a local run, **[Reported]** stated by
the project owner and not re-checked, **[Assumption]**, **[Open]** needs a check or decision.

## 1. Facts, assumptions and open checks

| Item | Status |
|---|---|
| AWS account `851725545966`, region `eu-west-1`, domain `ai-autofy-general-server.com` | [Reported] by the owner; also coded as guards in `infrastructure/energy_config.py` [Confirmed] |
| New hostnames `energy-api.` (API) and `energy.` (frontend) | [Reported]; validated by CDK code [Confirmed] |
| Game hostnames `api.` and `play.` belong to the Bluff or Believe game and must not be touched | [Reported] |
| Existing S3 data lake bucket `irish-energy-data-platform-851725545966`, prefixes `bronze/`, `silver/`, `gold/eirgrid/daily_demand/` | [Reported]; used in code [Confirmed] |
| Glue database `irish_energy_analytics`, table `daily_electricity_demand`, Athena workgroup `irish-energy-analytics` deployed | [Reported]; README says unestablished [Open] |
| Developer profile `irish-energy-data-platform-dev` can read Gold and (per a live run) write the lake layers | Read [Confirmed once]; write [Reported] |
| Developer profile is denied Glue and Athena | [Reported] |
| All other permissions of that profile | [Open] none verified |
| Hosting: ECS Fargate behind the ALB (API), CloudFront + S3 (frontend) | [Assumption] chosen in the plan; the alternative is API Gateway + Lambda |
| Pipeline compute: Lambda container image (ADR) versus Fargate task | [Open] decision |
| Whether the game uses a shared CDK bootstrap stack in this account/region | [Open] check before bootstrapping |
| Whether the game's hosted zone is the one for the domain | [Assumption] same public zone |
| API container image | built and run locally with Docker (521 MB, non-root, read-only root, health check, 401 without a token) [Confirmed]; not pushed anywhere |
| Pipeline execution | disabled in code and must stay disabled until step 12 |

## 2. AWS resources to create

Resources marked "CDK exists" synthesize today. "Spec only" and "to write" need new CDK
or administrator work. Name all new resources with an `irish-energy` prefix.

| Resource | Source |
|---|---|
| ACM certificate, eu-west-1, `energy-api.ai-autofy-general-server.com` | manual |
| ACM certificate, us-east-1, `energy.ai-autofy-general-server.com` | manual |
| Route 53 validation CNAMEs and alias records | manual (section 6) |
| VPC (2 public subnets, no NAT), internet gateway | CDK exists (`IrishEnergyApiEdgeStack`) |
| Internet-facing ALB, 443 listener, 80 redirect, host rule | CDK exists |
| Target group (port 8000, `/health`), ALB egress to the task port, task security group, S3 gateway VPC endpoint | CDK exists (inside `IrishEnergyApiEdgeStack`, included when the service inputs are given) |
| ECR repository `irish-energy-api` (scan on push, AES-256, immutable tags, lifecycle) | CDK exists (`IrishEnergyApiRegistryStack`, always synthesized, needs no certificates) |
| ECS cluster, Fargate task definition and service for the API, log group (30 days), execution and task roles | CDK exists (`IrishEnergyApiEdgeStack`) |
| ECR repository for a pipeline image | to write (depends on the pipeline compute decision) |
| DynamoDB run-history table `irish-energy-run-history` (keys `pk`/`sk`, index `gsi1`, TTL on `expires_at`, AWS-managed-key encryption, point-in-time recovery, deletion protection, retained) | CDK exists (`IrishEnergyRunHistoryStack`, always synthesized, no certificates needed; built from `infrastructure/run_control/spec.py`) |
| Step Functions state machine, execution log group | spec only |
| Pipeline stage compute (four Lambda container functions or Fargate task definitions) | to write; decision open |
| CloudWatch log groups with retention, alarms, AWS Budget | to write |
| Cognito user pool, public app client (no secret), Hosted UI domain, groups `viewer` and `operator` | manual or new CDK |
| S3 site bucket, CloudFront distribution, origin access control, response headers policy | CDK exists (`IrishEnergyFrontendStack`) |
| S3 gateway VPC endpoint | CDK exists (with the service) |
| DynamoDB gateway VPC endpoint | to write (free; recommended: keeps run-history traffic off the internet) |
| Glue database/table, Athena workgroup | CDK exists (`IrishEnergyDataLakeStack`) |

Never create, modify or replace the game's ALB, CloudFront distribution, certificate,
or `api.`/`play.` records.

## 3. IAM roles and policies

Full policy documents are generated by `infrastructure/run_control/spec.py`
(`api_task_policy`, `state_machine_role_policy`, `pipeline_stage_role_policy`); an
audit table is in the readiness review (section 5). Summary:

| Role | Trusted by | Allowed |
|---|---|---|
| API task role (CDK, this phase) | `ecs-tasks.amazonaws.com` (account-conditioned) | `s3:ListBucket` on the bucket for prefix `gold/eirgrid/daily_demand/*` and `s3:GetObject` on that prefix, plus, on the run-history table only: `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` on the table ARN and `dynamodb:Query` on the `gsi1` index ARN (exactly what `src/dynamodb_run_store.py` calls; `PutItem` also covers its write transaction). No Scan, Batch, table-management, KMS, Step Functions, Glue, Athena, IAM or Cognito permissions. `states:*` is attached only at activation |
| ECS task execution role (CDK) | `ecs-tasks.amazonaws.com` (account-conditioned) | pull from the one API repository (`ecr:BatchCheckLayerAvailability`, `GetDownloadUrlForLayer`, `BatchGetImage`), `ecr:GetAuthorizationToken` (resource `*` is unavoidable for this action), `logs:CreateLogStream` and `PutLogEvents` on the one log group; `secretsmanager:GetSecretValue` on exact ARNs only if secrets are ever configured |
| State machine role | `states.amazonaws.com` | `UpdateItem`, `DeleteItem` on the table; invoke the stage functions (or `ecs:RunTask` and `iam:PassRole` if Fargate); log delivery |
| Pipeline stage role | Lambda or ECS tasks | `GetObject` bronze/silver, `PutObject` bronze/silver/gold, Glue `GetTable` and `CreatePartition` on one table |
| Frontend deployer | an administrator or CI | site bucket upload/delete/list; `cloudfront:CreateInvalidation` on one distribution |

Do not grant wildcard actions or `Resource: "*"`. Do not widen the developer profile.
Remove the `ReadGoldForRegistration` statement from the stage role (not needed).

## 4. Certificates and domains

Two **separate** certificates, each for one exact name, DNS validated:

- eu-west-1: `energy-api.ai-autofy-general-server.com` (used by the ALB)
- us-east-1: `energy.ai-autofy-general-server.com` (used by CloudFront)

Do not reuse or assume the game's certificate. See
`docs/deployment-dns-and-certificates.md` for steps.

## 5. Configuration values

**API container** (all `[Confirmed]` names from `src/settings.py` and related code):

| Variable | Value |
|---|---|
| `API_ENV` | `production` (startup fails without Cognito settings) |
| `PORT` / `HEALTH_CHECK_PATH` | `8000` / `/health` |
| `AWS_DEFAULT_REGION` | `eu-west-1` |
| `FORWARDED_ALLOW_IPS` | the VPC CIDR (set by CDK); never `*`, so clients cannot forge their address |
| `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` | from the new Cognito pool |
| `CORS_ALLOWED_ORIGINS` | `https://energy.ai-autofy-general-server.com` |
| `S3_BUCKET` | `irish-energy-data-platform-851725545966` |
| `RUN_HISTORY_BACKEND` / `RUN_HISTORY_TABLE` / `RUN_HISTORY_REGION` | `dynamodb` / `irish-energy-run-history` (set by CDK from the table) / `eu-west-1`. **Required in production:** with `API_ENV=production` the app refuses to start unless these are set, and never falls back to SQLite. SQLite (`RUN_HISTORY_DB_PATH`) remains for local development only |
| `RATE_LIMIT_READ_PER_MINUTE`, `_ANALYTICS_PER_MINUTE`, `_WRITE_PER_HOUR`, `_AUTH_FAILURES_PER_MINUTE` | optional; defaults 60, 30, 5, 20 |
| `ANALYTICS_CACHE_TTL_SECONDS`, `EXPECTED_INTERVAL_MINUTES` | optional; defaults 300, 15 |
| `PIPELINE_EXECUTION_ENABLED` | leave unset; the app refuses `true` |
| `ENABLE_API_DOCS` | leave unset (refused in production) |
| AWS credentials | none; use the task role |

Run uvicorn with `--proxy-headers --forwarded-allow-ips=<ALB subnet range>`. The
image must add `boto3`, `pandas` and `pyarrow` to the API dependencies.

**Frontend build** (`frontend/.env.example`): `VITE_API_BASE_URL=https://energy-api.ai-autofy-general-server.com`,
`VITE_COGNITO_USER_POOL_ID`, `VITE_COGNITO_APP_CLIENT_ID`, `VITE_COGNITO_DOMAIN`,
`VITE_COGNITO_REDIRECT_SIGN_IN` and `_SIGN_OUT` (both `https://energy.ai-autofy-general-server.com/`),
optional `VITE_COGNITO_SCOPES`. Never include a client secret.

**CDK context** (not committed): `-c apiCertificateArn=...` (eu-west-1),
`-c frontendCertificateArn=...` (us-east-1), and, to include the ECS service,
`-c apiImageTag=<version tag, not latest>`, `-c cognitoUserPoolId=...`,
`-c cognitoAppClientId=...` (all three together). The Cognito IDs are identifiers, not
secrets. The API needs **no** secrets, so Secrets Manager is not used or granted.

## 6. DNS records

| Record | Type | Value |
|---|---|---|
| validation for `energy-api.` | CNAME | from the eu-west-1 certificate |
| validation for `energy.` | CNAME | from the us-east-1 certificate |
| `energy-api.ai-autofy-general-server.com` | A (AAAA if dual stack) alias | ALB: outputs `AlbDnsName`, `AlbCanonicalHostedZoneId` |
| `energy.ai-autofy-general-server.com` | A and AAAA alias | CloudFront `DistributionDomainName`; alias zone ID `Z2FDTNDATAQYW2` |

Only add new records. Check the names do not exist first.

## 7. Deployment sequence and dependencies

Each step depends on those before it. Stop at any failed check.

| # | Step | Needs | Why this order |
|---|---|---|---|
| 1 | Administrator prerequisites: confirm permissions (section 11); decide bootstrap; commit source; remove plaintext key file | owner review | everything else assumes these |
| 2 | Issue both ACM certificates and add DNS validation records | step 1, hosted zone access | the ALB and CloudFront stacks need issued ARNs |
| 3 | Deploy `IrishEnergyApiRegistryStack` (the repository only), then build, scan and push the API image with an explicit version tag | step 1 | the ECS service needs an image; tags are immutable so every release needs a new tag |
| 4 | Deploy `IrishEnergyRunHistoryStack` (the table, built from `spec.table_definition`) | step 1 | the ECS service stack imports its name and ARN, so it must exist first; the state machine will need it too |
| 5 | Create the state machine, stage compute and roles (execution still disabled; do not attach `states:StartExecution` to the API role yet) | steps 3, 4 | defined early so activation later is a configuration step, but unused until step 12 |
| 6 | Deploy the ECS API service and ALB target group with the health check on `/health` | steps 2, 3, 4 | the ALB must forward `energy-api.` to a healthy service |
| 7 | Cognito: pool, public app client, Hosted UI domain, groups, test users, callback URLs for the energy origin | step 1 | the API (step 6 env) and frontend build need its IDs; can be done in parallel with 6 but must finish before 8 and 11 |
| 8 | Build the frontend with the IDs; deploy `IrishEnergyFrontendStack`; upload and invalidate | steps 2, 6, 7 | needs the us-east-1 certificate and the API hostname |
| 9 | Create the alias records for `energy-api.` and `energy.` | steps 6, 8 | records must not point at nothing |
| 10 | Read-only smoke tests (section 8) | step 9 | verifies the edge before any user |
| 11 | Authentication and authorization tests | steps 7, 10 | proves 401/403 behaviour with real tokens |
| 12 | **Separately approved:** activate pipeline execution | all above, a reviewed code change | not part of the handover; see below |

**Step 12 (not authorized by this handover).** Activation requires a new, reviewed
change: lift the settings guard, construct `StepFunctionsExecutor` with the state machine
ARN, attach `states:StartExecution` to the API role, schedule the reconciler, and add
tests. Do it only after steps 1-11 pass and the owner approves in writing. First run
supervised, one operator, with the kill switch (stop activating / revert) ready.

## 8. Deployment and validation commands

Run by the administrator; none was run here. Replace placeholders.

```bash
# Step 1: who am I (read-only)
aws sts get-caller-identity --profile <admin-profile>
# Check for an existing shared bootstrap stack BEFORE bootstrapping (read-only)
aws cloudformation describe-stacks --stack-name CDKToolkit --region eu-west-1 --profile <admin-profile>

# Step 2: certificates (names must be exact; DNS validation)
aws acm request-certificate --region eu-west-1 --domain-name energy-api.ai-autofy-general-server.com --validation-method DNS
aws acm request-certificate --region us-east-1 --domain-name energy.ai-autofy-general-server.com --validation-method DNS

# Steps 6 and 8: review, then deploy only the energy stacks
cd infrastructure
npx cdk diff IrishEnergyApiEdgeStack IrishEnergyFrontendStack \
  -c apiCertificateArn=<eu-west-1 arn> -c frontendCertificateArn=<us-east-1 arn>
npx cdk deploy IrishEnergyApiEdgeStack IrishEnergyFrontendStack --require-approval broadening \
  -c apiCertificateArn=<eu-west-1 arn> -c frontendCertificateArn=<us-east-1 arn>

# Step 3: registry first, then build and push the image (explicit version tag, amd64)
npx cdk deploy IrishEnergyApiRegistryStack
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin 851725545966.dkr.ecr.eu-west-1.amazonaws.com
docker build --platform linux/amd64 -t 851725545966.dkr.ecr.eu-west-1.amazonaws.com/irish-energy-api:v0.1.0 ..
docker push 851725545966.dkr.ecr.eu-west-1.amazonaws.com/irish-energy-api:v0.1.0
# Step 4: the run-history table (needs no context; deletion-protected and retained)
npx cdk diff IrishEnergyRunHistoryStack
npx cdk deploy IrishEnergyRunHistoryStack

# Step 6: include the service in the edge stack by adding
#   -c apiImageTag=v0.1.0 -c cognitoUserPoolId=<pool> -c cognitoAppClientId=<client>
# to the cdk diff / cdk deploy commands above (Cognito must exist first: step 7).

# Step 8: frontend
cd frontend && npm ci && npm run build   # with the VITE_* variables set
aws s3 sync dist/ s3://<SiteBucketName> --delete
aws cloudfront create-invalidation --distribution-id <DistributionId> --paths "/*"
```

`cdk bootstrap` is an administrator action and is only needed if the account/region is
not yet bootstrapped. If `CDKToolkit` already exists (for example for the game), do not
re-bootstrap without checking its version and effect on the game.

**Read-only validation** (step 10 and 11):

```bash
dig +short energy-api.ai-autofy-general-server.com ; dig +short energy.ai-autofy-general-server.com
curl -sS -o /dev/null -w "%{http_code}\n" https://energy-api.ai-autofy-general-server.com/health      # 200 once the service is healthy
curl -sS -o /dev/null -w "%{http_code}\n" http://energy-api.ai-autofy-general-server.com/health       # 301 to https
curl -sS -i https://energy-api.ai-autofy-general-server.com/api/runs                                  # 401, WWW-Authenticate: Bearer
curl -sS -i -H "Authorization: Bearer <viewer access token>" https://energy-api.../api/runs           # 200
curl -sS -i -H "Authorization: Bearer <viewer access token>" https://energy-api.../api/analytics/daily-demand
curl -sS -i -X POST -H "Authorization: Bearer <viewer access token>" -H "Content-Type: application/json" -d '{}' https://energy-api.../api/runs   # 403
curl -sS -i -X POST -H "Authorization: Bearer <operator access token>" -H "Content-Type: application/json" -d '{}' https://energy-api.../api/runs # 503 "Pipeline execution is disabled."
echo | openssl s_client -connect energy.ai-autofy-general-server.com:443 -servername energy.ai-autofy-general-server.com 2>/dev/null | openssl x509 -noout -subject -issuer
```

Use access tokens, not ID tokens. Confirm the game is unaffected before and after:
`curl -sSI https://api.ai-autofy-general-server.com` and `https://play.ai-autofy-general-server.com`
should return the same responses as before. The two POST checks use the disabled
endpoint on purpose and must not start anything.

### Run history in DynamoDB (Phase 9.2)

`IrishEnergyRunHistoryStack` creates one on-demand table. Key design: `pk`/`sk` strings;
run items `RUN#<id>`, one lock item `LOCK#pipeline`, idempotency items `IDEM#<hash>`; the
`gsi1` index (`gsi1pk`/`gsi1sk`, all attributes) lists runs newest first. TTL on
`expires_at` removes expired idempotency records (run records have no TTL). Encryption
at rest uses the AWS managed key `alias/aws/dynamodb` (no key to pay for or administer,
no extra IAM). The table is retained on stack deletion, has deletion protection and
point-in-time recovery, and the name is fixed so the task role and settings agree.

Startup safety: the container exits non-zero (ECS rolls the deployment back and the
ALB never receives traffic) if `API_ENV=production` is set without
`RUN_HISTORY_BACKEND=dynamodb` or a valid `RUN_HISTORY_TABLE` and `RUN_HISTORY_REGION`.
If the table is unreachable at runtime the API stays up, logs the failure and answers
run-history requests with a generic 503.

Migration: there is nothing to migrate; local SQLite history is development data and
execution is disabled, so the table starts empty. Rollback is described in section 9.

### Networking decision for the API tasks (no NAT)

The VPC has no NAT gateway and this phase does not add one. Options considered:

| Option | Works? | Extra monthly cost (approximate, same assumptions as section 10) |
|---|---|---|
| **A. Public subnets, public IP, locked-down security groups (chosen)**: task SG accepts only the ALB SG on port 8000 and allows only outbound 443; free S3 gateway endpoint | yes | about 3.4 EUR per task for the public IPv4 address |
| B. Private subnets, no NAT, VPC interface endpoints: `ecr.api`, `ecr.dkr`, `logs` (plus `secretsmanager` only if secrets are used) and the S3 gateway endpoint | **not viable as designed**: the API must download Cognito's signing keys from `cognito-idp.eu-west-1.amazonaws.com/<pool>/.well-known/jwks.json`; whether a Cognito endpoint serves that path is unverified, and without it every token is rejected | 3 interface endpoints in 2 zones is about 44 EUR plus data processing |
| C. Private subnets with a NAT gateway | yes | about 35 EUR or more plus data processing |

Option A is the cheapest and works; its trade-off is that tasks hold public IPs, which
the security groups make unreachable except through the ALB. If policy requires
private tasks, choose C. Do not add a NAT gateway silently.

## 9. Rollback

0. API service only: redeploy the edge stack without the service inputs (it returns to the 503 placeholder), or set the desired count to 0.
1. Remove the two alias records (`energy-api.`, `energy.`) only.
2. Destroy the energy stacks (`cdk destroy IrishEnergyFrontendStack IrishEnergyApiEdgeStack`).
   The site bucket is retained (empty and delete it separately); switch off ALB
   deletion protection first.
3. Remove the new certificates and their validation CNAMEs.
4. Run history: the DynamoDB table is retained (deletion protection, point-in-time recovery). Remove the service stack first; the table's stack cannot be destroyed while the service imports it, and the table itself must have deletion protection switched off before it can be deleted. Production cannot be rolled back to SQLite by configuration (the app refuses to start); use a local environment for SQLite.
5. A stuck active-run lock expires after 45 minutes, or delete the single item
   `LOCK#pipeline` after confirming no execution is running.
6. If execution was ever activated, revert the activation change first.

None of these steps touches the game's resources.

## 10. Expected monthly cost (approximate, EUR)

All figures are **approximate** and for planning only. Assumptions: eu-west-1 list
prices from memory (not looked up), about USD 1 = EUR 0.92; Linux x86 Fargate at about
USD 0.0445 per vCPU-hour and USD 0.0049 per GB-hour; ALB about USD 0.025 per hour
plus LCUs; public IPv4 addresses about USD 0.005 per hour each; one API task of 0.25
vCPU and 1 GB running continuously (730 h); 30 pipeline runs a month of about 10
minutes; negligible traffic; AWS free tiers apply (CloudFront 1 TB, Step Functions
4,000 transitions, 100 GB data transfer out, Cognito first 10,000 users). Verify with
the AWS Pricing Calculator.

| Resource | Low-usage portfolio (EUR / month) | Idle cost? |
|---|---|---|
| ECS Fargate API task (0.25 vCPU, 1 GB, 24/7) | about 10.8 | **yes** |
| Public IPv4 for the task | about 3.4 | **yes** |
| Application Load Balancer (hourly + minimal LCU) | about 19 | **yes** |
| Public IPv4 for the ALB (2 zones) | about 6.7 | **yes** |
| Pipeline tasks (Fargate, about 5 hours a month; near zero if Lambda) | about 0.3 | no |
| DynamoDB (on-demand, point-in-time recovery, tiny data) | about 0.1 | storage only |
| Step Functions (about 450 transitions, inside the free tier) | 0 | no |
| ECR (two repositories, a few images) | about 0.15 | storage only |
| S3 (frontend and data lake; small) | under 0.2 | storage only |
| CloudFront (free tier) | 0 | no |
| CloudWatch (logs about 0.3 GB, about 5 alarms) | about 0.6 to 1 | alarms |
| Data transfer (inside the free 100 GB; same-region S3 and DynamoDB traffic) | 0 | no |
| Cognito, ACM, Route 53 records (existing zone shared with the game) | 0 | no |
| **Total** | **about 42 (range 37 to 47)** | almost all of it is idle cost |

Costs that accrue with no traffic: the ALB and its two IPs (about 26), the API task and
its IP (about 14), alarms. Ways to reduce it: delete the ALB and service between
demos (saves most of it), use API Gateway HTTP API with Lambda instead of ECS and an
ALB (near zero idle cost, but needs different packaging), or run the API in Lambda and
keep only CloudFront and S3 (about 1 to 3). A NAT gateway (about 35 or more) is
deliberately not used.

## 11. Permissions: what is confirmed and what must be checked

| Needed for | Developer profile status |
|---|---|
| S3 read of Gold prefix | confirmed once in this work |
| S3 write Bronze, Silver, Gold | reported (live run) |
| Glue `GetTable`, `CreatePartition` | reported denied |
| Athena queries | reported denied |
| ACM (request, describe), Route 53 (list, change records) | not verified |
| EC2/VPC, ELB, ECS, ECR | not verified |
| DynamoDB table creation and use | not verified |
| Step Functions, Lambda | not verified |
| CloudFront, S3 site bucket | not verified |
| Cognito user pool management | not verified |
| CloudWatch Logs and alarms, Budgets | not verified |
| IAM role creation, CDK bootstrap | not verified; an administrator should do these |

Do not widen the developer profile; use an administrator identity for deployment.

## 12. Outstanding checks before starting

- [ ] Source committed; plaintext key file removed; key rotated if exposed.
- [ ] Pipeline compute decision (Lambda container or Fargate) recorded.
- [ ] Missing constructs written and synthesized (see readiness review, section 6).
- [ ] `requirements-api.txt` includes `boto3`, `pandas`, `pyarrow`; image built and scanned.
- [ ] Existing CDK bootstrap state in the account checked.
- [ ] Hosted zone confirmed; `energy-api.` and `energy.` names confirmed free.
- [ ] Budget alert created before deploying the ALB and task.
- [ ] The owner has approved the sequence; execution activation is a separate approval.
