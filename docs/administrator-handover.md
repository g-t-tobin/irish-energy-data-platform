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
| Pipeline compute: Lambda container images (four functions, one shared image) | [Decided] by the owner in Phase 9.4; the ECS service stays the API only |
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
| Step Functions Standard state machine `irish-energy-pipeline` and its role (`IrishEnergyWorkflowStack`) | CDK exists (Phase 9.5; not deployed, never executed); no execution log group (see Phase 9.5 section) |
| Pipeline stage compute: four Lambda container functions, shared image, one role each (`IrishEnergyPipelineStack`) | CDK exists (Phase 9.4; not deployed, image not published, never invoked) |
| CloudWatch log groups with retention, alarms, AWS Budget | to write |
| Cognito user pool, public app client (no secret), Hosted UI domain, groups `viewer` and `operator` | manual or new CDK |
| S3 site bucket, CloudFront distribution, origin access control, response headers policy | CDK exists (`IrishEnergyFrontendStack`) |
| S3 gateway VPC endpoint | CDK exists (with the service) |
| DynamoDB gateway VPC endpoint with a restrictive endpoint policy | CDK exists (with the service, in `IrishEnergyApiEdgeStack`; Phase 9.3; not deployed or verified against AWS) |
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
| State machine role (CDK, Phase 9.5) | `states.amazonaws.com` (account-conditioned) | `dynamodb:UpdateItem`, `DeleteItem` on the run-history table; `lambda:InvokeFunction` on the four stage function ARNs; nothing else (no log delivery) |
| Pipeline stage roles (four, CDK, Phase 9.4) | `lambda.amazonaws.com` (account-conditioned) | one role per stage, see "Pipeline stage Lambda functions (Phase 9.4)" |
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
| 5 | Deploy `IrishEnergyPipelineStack` then `IrishEnergyWorkflowStack` (execution still disabled; the API role carries the dormant `states:` statements described in the Phase 9.5 section) | steps 3, 4 | defined early so activation later is a configuration step, but unused until step 12 |
| 6 | Deploy the ECS API service and ALB target group with the health check on `/health` | steps 2, 3, 4 | the ALB must forward `energy-api.` to a healthy service |
| 7 | Cognito: pool, public app client, Hosted UI domain, groups, test users, callback URLs for the energy origin | step 1 | the API (step 6 env) and frontend build need its IDs; can be done in parallel with 6 but must finish before 8 and 11 |
| 8 | Build the frontend with the IDs; deploy `IrishEnergyFrontendStack` **in a separately supervised session** (not with the execution role); upload and invalidate | steps 2, 6, 7 | needs the us-east-1 certificate and the API hostname |
| 9 | Create the alias records for `energy-api.` and `energy.` | steps 6, 8 | records must not point at nothing |
| 10 | Read-only smoke tests (section 8) | step 9 | verifies the edge before any user |
| 11 | Authentication and authorization tests | steps 7, 10 | proves 401/403 behaviour with real tokens |
| 12 | **Separately approved:** activate pipeline execution | all above, a reviewed code change | not part of the handover; see below |

**Step 12 (not authorized by this handover).** Activation requires a new, reviewed change **and separate written approval**: lift the settings guard so that `PIPELINE_EXECUTION_ENABLED=true` is accepted, construct `StepFunctionsExecutor` with the state machine ARN, enable the reconciler schedule (disabled by default), and add tests; **and** the IAM administrator must replace the runtime permissions boundary with the activation variant `docs/iam/runtime-permissions-boundary-execution-activation.json`. The initial boundary denies `states:StartExecution` for every runtime role, so attaching a permission to the API role is neither sufficient nor the mechanism: the API task role already carries the statement in the CDK template, and it stays ineffective until the boundary is replaced. The activation variant allows it only for the API task role. See `docs/phase-9.7.2.1-boundary-hardening.md`, section 4. Do it only after steps 1-11 pass and the owner approves in writing. First run
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

# Step 6: review, then deploy only the API edge stack. IrishEnergyFrontendStack is NOT deployed here:
# step 8 is a separately supervised procedure (see the Phase 9.7.2.2 update below).
cd infrastructure
export CDK_DEFAULT_ACCOUNT=851725545966 CDK_DEFAULT_REGION=eu-west-1   # required by every cdk command below
npx cdk diff IrishEnergyApiEdgeStack \
  -c apiCertificateArn=<eu-west-1 arn> -c frontendCertificateArn=<us-east-1 arn>
npx cdk deploy IrishEnergyApiEdgeStack --require-approval broadening \
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

### DynamoDB gateway endpoint (Phase 9.3)

**Why:** the tasks have no NAT and run in public subnets; without an endpoint their
DynamoDB calls would travel over the public internet path. A gateway endpoint routes them
over the AWS network instead, with no NAT gateway.

**Where and for whom:** defined in `IrishEnergyApiEdgeStack` (the stack that owns the VPC),
next to the existing S3 gateway endpoint, only when the ECS service is included. It is
attached to the route tables of the two public subnets the ECS service uses (the same two
the S3 endpoint serves). No new VPC, subnet change, NAT, interface endpoint or change to the
tasks' public IP.

**Endpoint policy** (one statement, `Principal: "*"` as endpoint policies require; it only
narrows what the task role may already do):

| | |
|---|---|
| Statement `RunHistoryItemOperations` | `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` on the table ARN only |
| Statement `RunHistoryListIndexQuery` | `dynamodb:Query` on the `index/gsi1` ARN only |
| Not allowed | `Scan`, `BatchGetItem`, `BatchWriteItem`, table management (`CreateTable`, `DeleteTable`, `UpdateTable`, `DescribeTable`), `TransactGetItems`, `ConditionCheckItem`, everything else |

These are exactly the operations `src/dynamodb_run_store.py` uses: `get_item`, `update_item`,
`delete_item`, `query` on `gsi1`, and `transact_write_items` whose items are all Put.

**Transactions and IAM.** DynamoDB has no IAM action named `dynamodb:TransactWriteItems`.
Per the AWS documentation ("Using IAM with DynamoDB transactions"), permissions for the
`Put`, `Update`, `Delete` and `Get` items inside a transaction are governed by the
permissions of the underlying `PutItem`, `UpdateItem`, `DeleteItem` and `GetItem`
operations; `ConditionCheck` uses `dynamodb:ConditionCheckItem`. The store's transaction
contains only Put items, so `dynamodb:PutItem` on the table ARN (task role and endpoint
policy) authorises it; no transaction permission is granted on the index. If the store adds
Update, Delete or ConditionCheck items, the matching action must be added to both policies
(`tests/test_dynamodb_transaction_permissions.py` derives the required actions from what the
store really does and will fail until it is). If the store ever needs another
operation, the task role and this policy must both be changed and the reason documented.
The ECS task role is unchanged.

**Cost:** gateway endpoints have no hourly endpoint charge. DynamoDB request and storage
charges, and any other applicable AWS data-transfer charges, still apply.

**Status:** tested only through local CDK synthesis and offline template tests. It has not
been deployed and connectivity has not been verified against AWS. After deployment, check
the endpoint is `available`, associated with both route tables, and that run-history calls
succeed from a task.

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
2. Destroy the API edge stack (`cdk destroy IrishEnergyApiEdgeStack`); see items 7 to 10 for what can stop it. The frontend stack is deleted only in a supervised administrator session, never together with the edge stack by one routine command.
   The site bucket is retained (empty and delete it separately); switch off ALB
   deletion protection first.
3. Remove the new certificates and their validation CNAMEs.
4. Run history: the DynamoDB table is retained (deletion protection, point-in-time recovery). Remove the service stack first; the table's stack cannot be destroyed while the service imports it, and the table itself must have deletion protection switched off before it can be deleted. Production cannot be rolled back to SQLite by configuration (the app refuses to start); use a local environment for SQLite.
5. A stuck active-run lock expires after 45 minutes, or delete the single item
   `LOCK#pipeline` after confirming no execution is running.
6. If execution was ever activated, revert the activation change first.

7. **Failed first-time creation of a stateful stack** (`IrishEnergyDataLakeStack`, `IrishEnergyApiRegistryStack`,
   `IrishEnergyRunHistoryStack`). CloudFormation rolls back automatically but leaves what it may not delete. The table
   `irish-energy-run-history` and the repository `irish-energy-api` are retained by design. For the data lake stack the Glue database,
   the Glue table and the Athena workgroup also remain, because the execution role has no `glue:DeleteDatabase`, `glue:DeleteTable` or
   `athena:DeleteWorkGroup`; the stack then stops in a failed rollback state. A stack that ends in `ROLLBACK_COMPLETE` cannot be
   updated and must be deleted before redeploying.
8. **Deleting a stateful stack needs separate written approval.** The IAM policy documents do not grant `cloudformation:DeleteStack`
   on these stacks and `rollback-policy.json` explicitly denies it. This is a deliberate policy choice, not something AWS makes
   technically impossible: a separately approved permission could allow it. For the data lake stack the administrator first
   removes, with approval and after read-only confirmation, only the Glue and Athena objects the failed attempt created.
9. **Retained-resource name collisions.** Because the table and the repository have fixed names and are retained, redeploying the
   run history or registry stack after a failed first creation fails with an already-exists error until the orphan is dealt with by
   an approved cleanup or an administrator decision to adopt it (this handover does not specify an import procedure). A table
   holds run records: switch off deletion protection and point-in-time recovery only with approval, and never delete a table that
   holds records. A repository is deleted only after checking that no needed image is in it. Run the read-only name-conflict checks first.
10. **ECS task-definition deregistration (edge stack teardown).** `ecs:DeregisterTaskDefinition` is allowed only for the project
    family `IrishEnergyApiEdgeStackApiServiceTaskDefinition*`. Whether ECS honours resource-level scoping for it is unverified. If it
    does not, the permission fails closed and the teardown stops at the task definition. Do not widen the permission: the
    administrator deregisters the project's task-definition revisions in a supervised step and then continues the deletion.

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
- [x] Pipeline compute decision recorded: Lambda container images (Phase 9.4).
- [ ] Missing constructs written and synthesized (see readiness review, section 6).
- [ ] `requirements-api.txt` includes `boto3`, `pandas`, `pyarrow`; image built and scanned.
- [ ] Existing CDK bootstrap state in the account checked.
- [ ] Hosted zone confirmed; `energy-api.` and `energy.` names confirmed free.
- [ ] Budget alert created before deploying the ALB and task.
- [ ] The owner has approved the sequence; execution activation is a separate approval.

## Pipeline stage Lambda functions (Phase 9.4)

**Decision.** The owner confirmed AWS Lambda container images for the pipeline stages (resolving
the Lambda-versus-Fargate conflict in the ADR). Step Functions Standard will chain them in
Phase 9.5 (see the next section). Defined in CDK (`infrastructure/stacks/energy_pipeline_stack.py`,
stack `IrishEnergyPipelineStack`, always synthesized, no certificates needed). **Not deployed, the
image is not published, no function has ever been invoked, and nothing was verified against AWS.**
The ECS Fargate service remains the FastAPI API only and is unchanged.

| Stage | Function | Handler (`src/pipeline_stages.py`) | Existing code it runs |
|---|---|---|---|
| Ingestion (Bronze) | `irish-energy-pipeline-bronze` | `ingest_handler` | `fetch_demand_data`, `save_to_s3` |
| Bronze to Silver | `irish-energy-pipeline-silver` | `silver_handler` | `process_bronze_object` |
| Silver to Gold | `irish-energy-pipeline-gold` | `gold_handler` | `process_silver_object` |
| Glue registration | `irish-energy-pipeline-glue-registration` | `glue_handler` | `register_gold_partition` |

All four stages have real implementations; the handlers only validate input, call them and return
`{"key": ...}` (ingestion also returns `records`; registration returns `partition_created`). No
handler calls another stage. Input contract (the Phase 6 one): the whole Step Functions state as the
event, i.e. `run_id` (UUID), `days_back` (1-30, ingestion only) and the previous stage's result as
`bronze.output.key`, `silver.output.key` or `gold.output.key`. Keys are validated against the layer
prefix before use, errors are never caught (`StageInputError`, `StageConfigurationError` or the
underlying error fail the invocation), and the DynamoDB run history is written by the state
machine (Phase 6), not by these functions. Phase 9.6 made Bronze and Silver names run-specific, so a retry rewrites the same object; Glue registration is idempotent (`AlreadyExistsException` returns `partition_created: false`).

**Image.** One shared image (`Dockerfile.lambda`, base `public.ecr.aws/lambda/python:3.13`, x86_64)
holds `src/` plus pandas, pyarrow and requests (`requirements-lambda.txt`); boto3 comes from the
Lambda runtime. Each function selects its handler with a command override, so there is one large
dependency layer, not four. Local build and checks (no push, no AWS):

```
docker build --platform linux/amd64 -f Dockerfile.lambda -t irish-energy-pipeline:local --load .
docker run --rm --entrypoint python irish-energy-pipeline:local -c "import src.pipeline_stages"
docker run -d --rm -p 9000:8080 irish-energy-pipeline:local src.pipeline_stages.silver_handler
curl -XPOST http://localhost:9000/2015-03-31/functions/function/invocations -d '{"run_id":"bad"}'
```

Only invalid inputs should be sent this way: a valid ingestion event would call EirGrid and S3.

**Initial sizes** (estimates, not measured; configured in `infrastructure/energy_config.py`,
`PIPELINE_STAGES`; adjust after the first supervised run). Ephemeral storage stays at the 512 MiB
default because every stage processes Parquet and JSON in memory. Each timeout is below the 600 s
the Phase 6 state machine allows a task and far below Lambda's hard 900 s limit.

| Stage | Memory | Timeout | Reasoning |
|---|---|---|---|
| Bronze | 256 MiB | 120 s | one HTTP request (30 s timeout) and a small JSON upload; no pandas work |
| Silver | 1024 MiB | 300 s | pandas and pyarrow parse the JSON and write Parquet |
| Gold | 1024 MiB | 300 s | reads Silver Parquet and aggregates |
| Glue registration | 512 MiB | 60 s | two Glue calls, but the shared module imports pandas at start-up |

**Execution roles** (one per function, trusted by `lambda.amazonaws.com` for this account only, no
managed policies; every statement is `Allow` with no conditions):

| Stage | Permissions |
|---|---|
| all | `logs:CreateLogStream`, `logs:PutLogEvents` on the function's own log group (30-day retention) |
| Bronze | `s3:PutObject` on `bronze/eirgrid/demand_actual/*` |
| Silver | `s3:GetObject` on `bronze/eirgrid/demand_actual/*`; `s3:PutObject` on `silver/eirgrid/demand_actual/*` |
| Gold | `s3:GetObject` on `silver/eirgrid/demand_actual/*`; `s3:PutObject` on `gold/eirgrid/daily_demand/*` |
| Glue registration | `glue:GetTable`, `glue:CreatePartition` on the catalog, database `irish_energy_analytics` and table `daily_electricity_demand` (no S3) |

No bucket-level or `s3:ListBucket` access (the handlers receive exact keys), no DynamoDB, Athena,
KMS, IAM, Cognito, EC2, Step Functions or ECR permissions, no `*` resources. The Glue catalogue
ARN is required by the Glue API alongside the database and table. Functions are not attached to a
VPC, so they reach EirGrid over the public internet without a NAT gateway. If the bucket is later
encrypted with a customer-managed KMS key, key permissions will be needed (not assessed: the bucket
is imported, not managed here). `tests/test_energy_pipeline_infrastructure.py` runs the handlers
against recording doubles and checks every S3 and Glue call against the role of the stage that made it.

**Image publishing.** The image is a CDK image asset (one asset shared by all four functions);
synthesis stages the build context but makes no AWS calls and builds nothing. The asset is
published to the CDK bootstrap repository when `cdk deploy` runs, which needs the account to be
bootstrapped (state not checked) and a deployer allowed to push to it. A separate ECR repository was
not created, and the API repository `irish-energy-api` is untouched. The bootstrap repository policy
is what lets Lambda pull the image; confirm this on a bootstrapped account.

**Outputs for Phase 9.5** (CloudFormation exports from `IrishEnergyPipelineStack`):
`IrishEnergyPipeline-BronzeFunctionArn`, `...-SilverFunctionArn`, `...-GoldFunctionArn`,
`...-GlueRegistrationFunctionArn`. The Phase 9.5 state machine role imports them and holds
`lambda:InvokeFunction` on those four ARNs plus DynamoDB `UpdateItem`/`DeleteItem` on the run table.

**Deployment prerequisites.** Administrator confirmation that the deployer may create Lambda
functions, IAM roles and log groups and publish the image asset; `glue:GetTable` and
`glue:CreatePartition` on the Glue table (not weakened; if unavailable the registration stage cannot
work); the `irish_energy_analytics` database and `daily_electricity_demand` table must exist
(`DataLakeStack`); Lambda limits: 15 minutes per invocation, so a stage that outgrows the sizes
above must be redesigned, not given a longer timeout. Pipeline execution remains disabled; the API
the stage roles have no Step Functions permission; the state machine (Phase 9.5) is described below.

## Step Functions workflow (Phase 9.5)

Defined in CDK (`infrastructure/stacks/energy_workflow_stack.py`, stack `IrishEnergyWorkflowStack`,
always synthesized, needs no certificates). **Only synthesised and tested locally: not deployed,
never executed, nothing verified against AWS.** Deploy order: run-history, pipeline, workflow, then
the API edge stack, which depends on the workflow stack.

**Architecture.** One **Standard** state machine `irish-energy-pipeline`, generated by
`infrastructure/run_control/spec.py` (`build_state_machine_definition`) so the Phase 6 design and
its tests stay the single source. The four existing Lambda functions are imported through the
Phase 9.4 exports (`IrishEnergyPipeline-<Stage>FunctionArn`); none is duplicated. Logging, tracing and
encryption settings are not enabled, so there is no CloudWatch Logs delivery and no wildcard
resource; the Standard execution history (kept by Step Functions for 90 days) and the run record are
the audit trail. Add log delivery later if wanted (it needs `logs:*Delivery*` actions on `*`).

**Execution input** (unchanged from Phase 6; `StepFunctionsExecutor` starts the execution named after
the run ID, so a repeated start is a no-op): `{"run_id": "<uuid>", "days_back": 1-30}`.

**Order and data flow.** `MarkRunning` (DynamoDB) then `Bronze`, `Record_bronze`, `Silver`,
`Record_silver`, `Gold`, `Record_gold`, `GlueRegistration`, `MarkSucceeded`, `ReleaseLockOnSuccess`,
`Succeeded`. Every stage uses the optimised `arn:aws:states:::lambda:invoke` with `Payload.$ = "$"`
(the whole state), `ResultSelector {"output.$": "$.Payload"}` (drops the Lambda response metadata)
and `ResultPath $.<stage>`; the handlers therefore read `bronze.output.key`, `silver.output.key` and
`gold.output.key`, and `run_id` and `days_back` stay in the state to the end. The DynamoDB tasks set
`ResultPath: null`, which matters: without it the DynamoDB response would replace the state
(found while testing; the CDK/jsii layer drops `None`, so the definition is serialised with
`json.dumps`). `tests/test_energy_workflow_infrastructure.py` runs the synthesised definition
against the real handlers and the DynamoDB test double to check these paths.

**Retry and failure.** Each Lambda task has a timeout (600 s, above the function timeouts) and
retries only Lambda service and invocation errors (`Lambda.ClientExecutionTimeoutException`,
`ServiceException`, `AWSLambdaException`, `SdkClientException`, `TooManyRequestsException`): at most
2 retries, 5 s then 10 s. Function errors such as `StageInputError`, `StageConfigurationError`, an
permanent EirGrid HTTP error (4xx other than 429) or a function timeout are **not** retried (Phase 9.6 retries transient EirGrid errors in ingestion only): they go to `Catch`, which records the
failure and ends the execution as `FAILED` (`Error: PipelineFailed`; the original error is kept in
the failing state's history events). A failure in Bronze, Silver or Gold marks the run `failed`; a
failure in Glue registration, or in recording the Gold key, marks it `partial_failure` (Gold exists).
Each failure releases the lock and the run record carries only the fixed text "A pipeline stage
failed." with `error_code` `INTERNAL_ERROR`; the stage that failed stays in `stage`. A missed lock
release is deliberately ignored (the lease expires) but cannot turn a failure into a success.
**Duplicate outputs and ingestion retries:** superseded by Phase 9.6 (run-specific Bronze and Silver names, retry of transient EirGrid errors); see "Pipeline contracts and local integration testing (Phase 9.6)".

**DynamoDB progress** (native `dynamodb:updateItem` / `deleteItem`, no extra Lambda). Keys are those
of the store: run item `pk = RUN#<run_id>`, `sk = RUN`; lock item `pk = LOCK#pipeline`, `sk = LOCK`.
The workflow only `SET`s `status`, `stage`, `started_at`, `bronze_key`, `silver_key`, `gold_key`,
`error`, `error_code`, `finished_at` and `updated_at`, using the same guards as
`DynamoRunStore.update_run` (progress only while `queued` or `running`; a terminal status also over
`interrupted`). It never writes identity, `days_back`, `created_at`, `requested_by`, the listing index
keys, TTL or `lease_expires_at`, and never uses `PutItem`. Status values are `running`, `succeeded`,
`failed` and `partial_failure`; stages are `bronze`, `silver`, `gold`, `glue_registration` and
`completed`. The lock is released with a delete conditioned on `run_id = :rid`. Difference from the
store: the store's progress updates also extend the lock lease (heartbeat); the workflow does not.
This is safe because the whole execution is limited to 30 minutes and the lease is 45 minutes from
`create_run`; if the workflow timeout is ever raised past the lease, add a lease update. Workflow
timestamps are `$$.State.EnteredTime` (`...Z`), the store's are `+00:00`; both are ISO 8601 UTC.

**State machine role** (separate from the four Lambda roles; trusted by `states.amazonaws.com` for
this account only; no managed policies): `dynamodb:UpdateItem` and `dynamodb:DeleteItem` on the
run-history table ARN (not the index), and `lambda:InvokeFunction` on the four unqualified function
ARNs (an unqualified function ARN needs no `:*`, per the AWS documentation; versions and aliases are
not used). No S3, Glue, Athena, IAM, Cognito, EC2, KMS or logging permissions and no wildcard
resources.

**API task role.** Two statements were added, dormant while execution is disabled:
`StartAndInspectOneStateMachine` (`states:StartExecution` on the exact state machine ARN
`arn:aws:states:eu-west-1:851725545966:stateMachine:irish-energy-pipeline`) and
`ReconcileOwnExecutions` (`states:DescribeExecution` on `...:execution:irish-energy-pipeline:*`). The
Gold read and run-history permissions are unchanged, and there is no `states:*` or Lambda permission.
The container gets `PIPELINE_STATE_MACHINE_ARN` (validated at startup; no code reads it yet). The
ARN is derived from the fixed name in `infrastructure/energy_config.py`, not looked up. The ECS tasks
reach Step Functions over the internet path they already use for AWS APIs (no VPC endpoint was added).

**Execution stays disabled.** `PIPELINE_EXECUTION_ENABLED` is still refused by `load_settings`, the API
never constructs `StepFunctionsExecutor`, `POST /api/runs` still returns 503
`{"detail": "Pipeline execution is disabled."}` for an authenticated operator, and tests, application
start-up and CDK synthesis create no AWS client and start no execution.

**Before execution can be enabled:** deploy and bootstrap (see the Phase 9.4 prerequisites), publish
the Lambda image, confirm the Glue and Step Functions permissions with the administrator, run the
workflow once by hand with a test input after review, decide on execution logging and alarms, then a
separate reviewed change: lift the settings guard, construct the executor from
`PIPELINE_STATE_MACHINE_ARN`, wire the reconciler and re-test. Nothing in this document claims any of
that has happened.

## Pipeline contracts and local integration testing (Phase 9.6)

**Everything in this section was verified with local doubles only (files for S3, in-memory Glue and
DynamoDB, a Step Functions client double and a local interpreter of the synthesised state machine).
Nothing was deployed or run against AWS, and none of this replaces a supervised first run in AWS.**

### Contracts (as found, unchanged except where stated)

| Item | Contract |
|---|---|
| Execution input | `{"run_id": "<lowercase UUID>", "days_back": 1-30}`; the execution name is the run ID |
| Stage input | the whole workflow state: `run_id`, `days_back` and each earlier result as `<stage>.output` |
| Stage output | `bronze`: `{"key", "records"}`; `silver`, `gold`: `{"key"}`; `glue_registration`: `{"key", "partition_created"}` |
| Run record | `pk RUN#<run_id>`, `sk RUN`; `run_id`, `status`, `stage`, `days_back`, `created_at`, `updated_at`, `requested_by`, `gsi1pk`, `gsi1sk`, plus `started_at`, `finished_at`, `bronze_key`, `silver_key`, `gold_key`, `error`, `error_code` as the run progresses |
| Lock | `pk LOCK#pipeline`, `sk LOCK`, `run_id`, `lease_expires_at`; taken with the run in one transaction; renewed by each progress update made through the store (not by the workflow); released by a terminal update or a delete conditioned on the owning `run_id`; a lease older than 45 minutes can be taken over |
| Statuses | active `queued`, `running`; terminal `succeeded`, `failed`, `partial_failure`, `interrupted` |
| Error codes | `ACCESS_DENIED`, `UPSTREAM_UNAVAILABLE`, `VALIDATION_FAILED`, `STORAGE_ERROR`, `INTERRUPTED`, `INTERNAL_ERROR`; the workflow always writes `INTERNAL_ERROR` with fixed text |
| Executor | `StepFunctionsExecutor.start` records the run, then starts one execution; a start failure fails the run and frees the lock; `reconcile_active_runs` settles runs still active from `DescribeExecution` (not scheduled or wired) |

### What changed in this phase

1. **Run-specific Bronze and Silver names.** `bronze/.../ingestion_date=<date>/demand_actual_<run_id>.json`
   and the same for Silver `.parquet` (the partition layout and prefixes are unchanged; the stage
   handlers pass the run ID; calls without a run ID keep the timestamped names, which is what the
   standalone commands and the API's local executor still use). IAM is unchanged because the
   prefixes are the same.
2. **Transient EirGrid errors are retried.** `TransientEirGridError` (timeouts, connection errors and HTTP
   429, 500, 502, 503, 504) is raised by `src/ingestion/eirgrid.py`; the ingestion state retries it by name
   3 times at 10 s, 20 s and 40 s, separately from the Lambda service-error retries (2 retries, 5 s and 10 s).
   Everything else is permanent and is never retried: other HTTP 4xx, TLS errors, invalid input, bad or
   unexpected response bodies. Worst-case ingestion is about 190 s of the 1800 s workflow limit.
3. **Defect fixed:** `safe_error_message` raised `AttributeError` for any `requests` exception (their
   `response` is not a dict), which would have stopped the API's local executor recording the failure.

### Data integrity findings

| Question | Finding |
|---|---|
| Does a retry duplicate Bronze or Silver? | Before: yes (timestamped names). Now: no, the retry rewrites the run's own object (a retry that crosses 00:00 UTC lands in the next date partition and leaves the first object behind) |
| Does a retry duplicate Gold? | No. Gold is one object per run date (`daily_demand.parquet`) and the same Silver gives identical content |
| Can a retry make the next stage read the wrong object? | No. Each stage reads the exact key in the state; no stage looks up "the latest" object |
| Can a failed run leave partial data? | Not partial objects (S3 puts are atomic), but completed earlier layers stay: a Gold failure leaves Bronze and Silver. Nothing cleans them up |
| Can concurrent runs interfere? | Not in Bronze and Silver (run-specific names, tested with 8 threads). The run lock allows one active run anyway |
| Do two runs on the same day overwrite each other's Gold? | **Yes, by agreed policy.** Gold keeps one canonical object per run date, `gold/eirgrid/daily_demand/run_date=YYYY-MM-DD/daily_demand.parquet`; the latest successful run for a date replaces the previous Gold dataset. The Glue partition layout and the analytics reader are unchanged, and per-run Gold history is not kept (Bronze and Silver keep run-specific objects) |

### Test architecture

`tests/pipeline_harness.py` wires the real handlers, processing code, DynamoDB store, executor and the
CDK-synthesised definition to doubles: `LocalDirS3` (files under a temporary directory), a recording Glue
double, `FakeDynamoDB`, a scripted `requests.get` returning real `Response` objects and
`LocalStepFunctions` (runs the workflow in-process, immediately or when told). `tests/asl_simulator.py`
interprets only the states this definition uses; it does not model time, `TimeoutSeconds`, payload size limits or
redrive, never sleeps (delays are recorded), and treats an unresolvable path as `States.Runtime`, which
`States.ALL` does not catch (per the AWS documentation). Test modules: `test_pipeline_integration`,
`test_pipeline_idempotency`, `test_eirgrid_retry_classification`, `test_step_functions_failure_simulation`,
`test_run_history_reliability`, `test_executor_integration`, `test_pipeline_security_regression`.

### Known limitations (Phase 9.6; items marked "9.6.1" are addressed in the next section)

- 9.6.1: a successful workflow whose final lock release fails leaves the finished run holding the lock; the
  reconciler now releases it once the execution is over (otherwise new runs are refused until the lease expires).
- 9.6.1: a `States.Runtime` failure ends the execution without marking the run; the reconciler settles it
  (needs the reconciler to run: scheduled, once separately approved).
- If the record of the Gold key cannot be written, the run is `partial_failure` without a `gold_key`, so the
  registration command needs the key found by hand (`gold/eirgrid/daily_demand/run_date=<date>/`).
- 9.6.1: runs marked `interrupted` are now re-checked for 24 hours, so the real outcome replaces a restart's guess.
- The workflow does not renew the lock lease; this is safe only while the workflow limit stays below the lease.
- Failed attempts and failed runs leave Bronze and Silver objects behind; Bronze and Silver have no retention rule.
- Workflow timestamps end in `Z` and the store's in `+00:00`.
- The API's local-thread executor (`src/api.py`) is unchanged and keeps timestamped names.
- Real behaviour of Lambda, Step Functions, DynamoDB and Glue (permissions, quotas, timing, error names such as the
  Lambda `errorType` of `TransientEirGridError`) is assumed from documentation and untested.

### Deployment prerequisites still open

Everything in the Phase 9.4 and 9.5 sections: bootstrap and deployment, image publication, administrator
confirmation of Glue and Step Functions permissions, a supervised test run, logging and alarms, scheduling the
reconciler, and a separate reviewed change to activate execution.

## Automatic reconciliation and lock recovery (Phase 9.6.1)

**Local only: designed, implemented and tested against test doubles. Nothing was deployed or run against AWS, and
pipeline execution is still disabled (`PIPELINE_EXECUTION_ENABLED=true` still stops start-up). The scheduler is not
created and, when synthesised at all, is DISABLED.**

### Design

`src/reconciliation.py` (`reconcile`, `reconcile_run`) is the one implementation. It is used by the API-side
`reconcile_active_runs` / `StepFunctionsExecutor` and by the scheduled function `src/reconcile_runs.py`. It only calls
`DescribeExecution` and conditional writes on the run-history table; it never starts an execution and touches no S3,
Glue or Athena.

Each pass reads the lock and the 50 newest runs, then checks every active run, every run marked `interrupted` in the last
24 hours, and the run that holds the lock (even if old). The execution is found by name (the run ID), so no execution ARN
is stored. The execution status decides:

| Execution | Run record | Action |
|---|---|---|
| `RUNNING` / `PENDING_REDRIVE` | active | never interrupted; the lease is renewed (conditional on owning the lock) when under 20 minutes remain |
| `RUNNING` | already final, still holds lock | nothing (the workflow is releasing it) |
| `SUCCEEDED` | active or `interrupted` | `succeeded`, stage `completed`; the terminal update releases the lock |
| `FAILED` / `TIMED_OUT` | active or `interrupted` | `failed` (`partial_failure` if a `gold_key` was recorded), error code `INTERNAL_ERROR` |
| `ABORTED` | active | `interrupted` |
| any finished status | already final, holds lock | lock released (owner-conditioned delete) |
| does not exist | active | unchanged for 5 minutes after creation, then `interrupted` |
| does not exist | final or `interrupted`, holds lock | lock released |
| query fails (throttling, permission, network) | any | nothing changes; counted as an error |
| a status the code does not know | any | nothing changes |

A pass never changes `requested_by`, `days_back`, the idempotency record, the listing keys or other run data, and the public
run format is unchanged. Repeating or running passes concurrently is safe because every write is conditional (tested with 6
concurrent passes).

### Lock recovery

| Situation | Behaviour |
|---|---|
| Final lock release failed after success or failure | the next pass sees a final run still holding the lock with a finished execution and releases it |
| Lease expired, execution still running | the lease is renewed; a new run is refused (`StepFunctionsExecutor` checks the holder before `create_run` and refuses unless the old run is provably over) |
| Lease expired, execution finished | the old run is settled, the lock released and the new run starts |
| Lease expired, execution missing | treated as above after the 5-minute grace period |
| Execution cannot be queried | nothing changes and a new run is refused (fail safe) |
| Another run's lock | `release_lock` and `renew_lock` are conditioned on the lock's `run_id`, so one run can never release or renew another's |

The 45-minute lease and 30-minute workflow timeout are unchanged: the workflow can never legitimately outlive its lease,
and renewal while `RUNNING` covers any future change. Limits: callers that use the store directly (not the executor)
bypass the expired-lock check, and a lease that expires in the instant between the check and `create_run` is only safe
because an execution cannot outlive its lease. Making the API call the reconciler at start-up (instead of
`recover_interrupted`, which looks at leases only) is part of the activation change, not done here.

### Scheduled reconciliation (optional, off by default)

`EnergyReconcilerStack` is synthesised only with `-c reconcilerEnabled=true`: one zip-packaged Python 3.13 function (256 MiB,
60 s, source only), its 30-day log group, and an EventBridge Scheduler schedule `rate(5 minutes)` with `State: DISABLED`
unless `-c reconcilerScheduleState=ENABLED` is also given (no retries, event age 60 s). Not deployed; deploying it needs the
CDK bootstrap bucket for the zip.

Minimum permissions (function role): `logs:CreateLogStream` and `PutLogEvents` on its own log group;
`states:DescribeExecution` on `arn:aws:states:eu-west-1:851725545966:execution:irish-energy-pipeline:*`;
`dynamodb:GetItem`, `UpdateItem`, `DeleteItem` on the run-history table ARN and `dynamodb:Query` on its `gsi1` index ARN.
Nothing else: no `StartExecution`, `PutItem`, transactions, `Scan`, S3, Glue, Athena, Cognito, IAM, ECS or wildcard
resources. The scheduler's role (trusted by `scheduler.amazonaws.com`, account-conditioned) can only
`lambda:InvokeFunction` on that one function. A test runs every reconciler path and checks the operations used equal the
actions granted.

### Lambda error names for retries

The ingestion retrier matches `TransientEirGridError`. Conclusion: no change was needed.
- Python's Lambda runtime client builds the error from `type(exc).__name__`, the unqualified class name. Checked locally in
  the Phase 9.4 image (`awslambdaric.bootstrap.build_fault_result` returns `errorType: TransientEirGridError`), without
  invoking any function.
- The AWS Step Functions tutorial "Handling error conditions in a Step Functions state machine" catches a Lambda-thrown
  custom error through `arn:aws:states:::lambda:invoke` with `ErrorEquals: ["CustomError"]` and retries it alongside the
  `Lambda.*` service errors. The best-practices page lists `Lambda.ClientExecutionTimeoutException`,
  `Lambda.ServiceException`, `Lambda.AWSLambdaException` and `Lambda.SdkClientException` (plus `Lambda.TooManyRequestsException`
  per the error-names page), which is exactly the service-error retrier here.
- **Not verified on AWS.** The tutorial shows a Node.js function, not Python, and the error-names page says unhandled
  Lambda errors were historically reported as `Lambda.Unknown` (timeouts as `Sandbox.Timedout`). If a real execution
  reports `Lambda.Unknown` for this exception, the retrier would not match and ingestion would fail without retrying; that
  fails safe. `Lambda.Unknown` is deliberately not retried because it would also retry permanent errors. Confirm with one
  supervised execution that injects a transient failure.
- Non-retryable by design and by test: invalid input, other HTTP 4xx, TLS errors, bad or unexpected bodies, `Lambda.Unknown`,
  `Sandbox.Timedout`, `States.TaskFailed`. The 10 s, 20 s, 40 s schedule is unchanged.

### Operational monitoring (minimum)

The reconciler logs one JSON line per action (`reconcile.run`: run ID, statuses, stale flag, error class) and one summary
line in CloudWatch Embedded Metric Format (`IrishEnergy/Reconciler`, no dimensions: `RunsChecked`, `StaleActiveRuns`,
`RunsSettled`, `RunsInterrupted`, `LeasesRenewed`, `LocksReleased`, `RunsUnchanged`, `ReconcileErrors`, `DurationMs`;
failures log `reconcile.failed` with `ReconcileFailures`). Logs hold no messages, payloads, tokens or credentials. No alarms
or other resources were added; proposed alarms (to create separately):

| Need | Source (no new permissions) | Proposed alarm |
|---|---|---|
| Runs stuck active | `StaleActiveRuns` | greater than 0 for 2 periods |
| Reconciliation not running or failing | `ReconcileFailures`, `ReconcileErrors`, no data on `RunsChecked` | any failure; missing data |
| Stuck locks | `LocksReleased` | any value (a release failed in the workflow) |
| Execution failures | `AWS/States` `ExecutionsFailed`, `ExecutionsTimedOut`, `ExecutionsAborted` | greater than 0 |
| Ingestion retries | Step Functions history; Lambda `Errors` for `irish-energy-pipeline-bronze` | errors repeated |
| Glue registration failures | Lambda `Errors` for the Glue function; runs in `partial_failure` | any |
| Start or lock failures | API logs (`ExecutionStartError`, `ActiveRunError`) | log metric filter |

### Not verified on AWS

EventBridge Scheduler delivery and permissions, the zip deployment, `DescribeExecution` behaviour and rates, real Lambda error
names, EMF ingestion, and every IAM statement above. Remaining prerequisites: those of Phases 9.4 to 9.6, plus approval to
synthesise and deploy the reconciler, and the activation change (API calls the reconciler at start-up).

## Phase 9.7: deployment readiness and administrator actions

The full assessment, resource inventory, IAM matrix, findings, cost estimate, deployment sequence and rollback plan are in
`docs/phase-9.7-production-readiness.md`. Verdict: **ready after specified blockers are resolved.** Nothing was deployed or
inspected in AWS; every item below is an administrator action or a decision.

Blockers (Critical): confirm the deployment identity and a custom CloudFormation execution policy (not
`AdministratorAccess`), and check read-only whether CDK is already bootstrapped and whether the game shares that stack;
verify the supplied API certificate ARN (status, region, hostname coverage) and issue the `us-east-1` certificate for
`energy.ai-autofy-general-server.com` with DNS validation; create and verify the Cognito user pool, groups and public
app client (authorization-code grant with PKCE, callback and sign-out URL `https://energy.ai-autofy-general-server.com/`).

Before deployment (High): remove and rotate the plaintext AWS key file in the project directory; confirm Docker, bootstrap
repository policy and image publishing; confirm Glue and Lake Formation and whether the Glue database, table and Athena
workgroup already exist; decide the frontend publishing procedure and `VITE_*` build variables; accept or change the
unpinned data lake stack; verify from a task that Cognito, ECR and CloudWatch are reachable with public IPs and no NAT; verify
or drop the `aws:SourceAccount` condition on the Lambda role trust. Corrected deployment order: budget alert, permissions and
bootstrap review, certificates and DNS validation, Cognito, data lake check, ECR registry and API image push, run history,
pipeline, workflow, optional reconciler (schedule disabled), ECS and ALB, frontend, DNS aliases, HTTPS and sign-in
verification, read-only smoke tests; supervised pipeline verification only after separate approval.

## Phase 9.7.1 update

The data lake stack is now pinned to account `851725545966`/`eu-west-1` (the "unpinned data lake stack" item above is resolved);
a different active account stops synthesis. A production frontend build refuses to run without the production API URL and
Cognito settings. Still open for the administrator: the plaintext key file `aws-access-key.txt` (remove and rotate; contents were
never read), supervised verification of the Lambda trust condition, and all Critical items. See
`docs/phase-9.7.1-local-corrections.md`.

Correction: `CDK_DEFAULT_ACCOUNT=851725545966` must be set explicitly (with `CDK_DEFAULT_REGION=eu-west-1`) for every
`cdk synth`, `diff` or `deploy`; a missing, empty or blank value is refused, not just a different account.

## Phase 9.7.2 update

An administrator package is in `docs/iam/` with `docs/phase-9.7.2-administrator-preparation.md`: a restricted CDK deployer, a
CloudFormation execution role built from five resource-scoped policies (not `AdministratorAccess`), PassRole and ECR
publishing policies, a read-only verification policy and an optional rollback policy. Before anything else, run the read-only
bootstrap inspection (section 10 of that document) and decide on a dedicated qualifier and toolkit stack. The developer
profile gets no deployment permissions. Nothing was applied.

## Phase 9.7.2.1 update

Before the first deployment the IAM administrator must create the permissions boundary `irish-energy-runtime-boundary` from
`docs/iam/runtime-permissions-boundary.json` (the deployer and the execution role are explicitly denied any change to it) and
create `iam-role-management-policy.json` as the sixth execution policy. The application must then be told to use the boundary
(`@aws-cdk/core:permissionsBoundary` context, not yet applied; see `docs/phase-9.7.2.1-boundary-hardening.md`, section 8). Until
both are done, role creation fails by design. The API task role's `states:StartExecution` is inactive under the initial
boundary; activation needs the separate variant and approval. `docs/iam/frontend-publishing-policy.json` is optional and needs the
`SiteBucketName` and `DistributionId` outputs. Nothing was applied.

## Phase 9.7.2.2 update (isolation corrections)

- **Frontend stack is supervised.** The execution role has no CloudFront and no site-bucket permission, because those cannot be
  isolated from the game's distributions by IAM. The first deployment of `IrishEnergyFrontendStack` (bucket, bucket policy,
  distribution, origin access control, response headers policy) is a supervised session by the account owner with a
  time-boxed, separately approved permission set and these steps: read-only pre-checks by the session's own identity (no existing distribution with the alias,
  list existing origin access controls and response headers policies; not available to the general read-only policy); `cdk diff` of that one stack showing creations only;
  deploy that one stack by name, never `--all`; check CloudTrail afterwards; record `SiteBucketName`, `DistributionId` and the two
  policy identifiers; remove the session. Later updates may use `frontend-stack-update-policy.json` with those exact identifiers,
  attached temporarily. Uploading the site and invalidating the cache use `frontend-publishing-policy.json`. The package contains
  no policy that can create or delete a distribution.
- **Load balancer.** The application load balancer now has the fixed name `irish-energy-api` (a one-line change in the edge
  stack) so listener and rule permissions name it; they cannot match another load balancer. Confirm with
  `aws elbv2 describe-load-balancers` (read-only) that the name is unused before the edge stack is deployed.
- **Every runtime-role trust-policy change must be reviewed by hand.** `iam:UpdateAssumeRolePolicy` is not in normal deployment
  (CloudFormation needs it only when a role's trust policy changes in an update). If a `cdk diff` shows a trust-policy change,
  stop; the project owner and a security reviewer approve it; only then attach `iam-trust-update-policy.json` for that one
  deployment and detach it afterwards. The permissions boundary does not protect trust-policy contents, so the review is the
  only control. A possible trigger is the Lambda trust condition check (H-3).
- **Resource-based policies can bypass the boundary.** Before deployment run the read-only checks for the data lake bucket policy,
  both ECR repository policies, Lambda function policies and any DynamoDB resource policy, and remove grants to project role ARNs
  that the runtime design does not need.

## Phase 9.7.2.2 correction (client secrets, read-only scope, Glue deletion)

- The general read-only policy no longer allows `cognito-idp:DescribeUserPoolClient`, because the response can contain the client secret
  and `--query` filtering is not an IAM boundary. The energy app client must be inspected separately by the pool's owner using a
  separately authorised identity limited to that pool, without printing or recording the response (prefer the console). Pool and
  group checks use `list-user-pool-clients` and `list-groups` for the energy pool only (`list-user-pools` and `describe-user-pool` were later moved out of the general policy, see below). A client secret on the energy client is a finding: it must be a public client.
- Other read-only calls that could expose unrelated applications (`iam:ListRoles`, `cloudformation:ListExports`, CloudFront lists,
  `ssm:DescribeParameters`) were removed or scoped to project identifiers; the CloudFront pre-checks belong to the supervised frontend
  session.
- `glue:DeleteDatabase`, `glue:DeleteTable` and `athena:DeleteWorkGroup` were removed from the execution policy. If the first creation
  of the data lake stack fails after creating objects, the administrator cleans up only those objects with approval, then deletes the
  stack.

## Phase 9.7.2.3 update (documentation consistency)

Unresolved **administrator decisions** that must be made and recorded before the related step:

1. **Which identity carries the temporary frontend permissions.** The repository does not establish it: the CDK application uses the
   default synthesizer, no separate CloudFormation service role is defined or documented as existing, and the deployer may pass only the
   bootstrap execution role. Either (A) a separately approved policy is attached temporarily to the CloudFormation execution role, which
   widens that role for the session and removes IAM isolation from the game's CloudFront resources until it is detached, or (B) a new,
   separate CloudFormation service role is created by the IAM administrator and used for that stack only, which keeps the execution
   role narrow but needs administrator work outside this package (creating the role, a narrow `iam:PassRole` to it, a supervised check).
   In both cases isolation is procedural during the session. Frontend creation must not start until the choice is made. See
   `docs/phase-9.7.2-administrator-preparation.md`, section 14.1a.
2. How and by whom stateful stacks are deleted or cleaned up after a failed first creation (items 7 to 9 above).
3. Whether to adopt or delete retained resources that block a redeploy.
4. The bootstrap qualifier and the creation of the permissions boundary (see the earlier updates).

## Read-only policy tightening (Lambda and SSM)

- The general read-only policy no longer allows `lambda:GetFunction`, `lambda:GetFunctionConfiguration` or `ssm:GetParametersByPath`. Function
  configuration responses contain environment variable values, and `GetFunction` also returns a presigned URL to the deployment package.
- Inspect Lambda **resource policies** with `aws lambda get-policy --function-name <name> --query Policy --output text` for each of the five project functions.
- Inspect Lambda **configuration** only from the reviewed `cdk diff` and templates. If it must be checked after deployment, use a separately authorised identity
  and `aws lambda get-function-configuration` with a `--query` selecting non-secret fields; the query is not a security boundary, so never print,
  log, redirect or record the full response (prefer the console), and never run `aws lambda get-function`.
- Existing bootstrap qualifiers are found from the `Qualifier` parameter of the `CDKToolkit*` stacks (`describe-stacks`), then the exact version parameter is read with
  `ssm:GetParameter`. The general policy cannot list SSM parameters.
- The general policy cannot confirm that a function name is unused; the first creation of the pipeline or reconciler stack fails with an already-exists error and
  overwrites nothing. The shared hosted zone's records (including the game's) are readable with the energy-zone permission; this is public DNS data and an accepted limitation.

## Separate inspection policies (Phase 9.7.2.3)

Three read permissions are **not** in the general verification policy and each needs **explicit administrator approval** before it is attached, to a separate
`irish-energy-inspector` role, for one named inspection, then detached. **Read-only is not the same as safe.**

- `docs/iam/dns-inspection-policy.json`: `route53:ListResourceRecordSets` on the energy hosted zone (ID not yet known, placeholder). IAM cannot restrict it to
  record names, so it shows every record in the shared zone, including the game's. Before approval, decide whether the zone check is needed at all.
- `docs/iam/cloudformation-diagnostic-policy.json`: `cloudformation:DescribeStackEvents` on the eight project stacks only (never `CDKToolkit*`). Events can contain
  resource properties and configuration values; IAM does not prevent that. For routine checks use `DescribeStacks` and `DescribeStackResources`.
- `docs/iam/cognito-inspection-policy.json`: `cognito-idp:DescribeUserPool` on the energy pool (placeholder). It can reveal SMS and Lambda trigger settings. No client
  secret is available through it.
- The general policy no longer allows `cognito-idp:ListUserPools` either (each pool summary can include trigger configuration of other pools); take the pool ID
  from the Cognito owner's record.
- Safe use: read the JSON, attach for the named purpose, run the single matching command for the single resource, do not save or paste the output, detach.
  Resource scoping and the response contents are unverified until separately authorised testing.
