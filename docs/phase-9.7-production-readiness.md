# Phase 9.7: production readiness and deployment assessment

Status: assessment only. Nothing was deployed, no AWS API was called, no permission, DNS record or certificate was
inspected or changed, and pipeline execution is still disabled (`PIPELINE_EXECUTION_ENABLED=true` still stops start-up; the
reconciler schedule is disabled by default). Baseline: commit `41140a8` (Phase 9.6.1), account `851725545966`, region
`eu-west-1`.

Method: the code, the synthesised CloudFormation of every stack (offline, including the optional ones), existing
documentation and the tests. **Prices are estimates from memory, not verified.** Anything that depends on the real account is
marked *unverified* and treated as a blocker or a required check, never assumed to work.

Update (Phase 9.7.1): H-7 and L-1 are resolved and H-6 is partly mitigated (production builds now fail without the
required settings); see `docs/phase-9.7.1-local-corrections.md`. The remainder of this report is unchanged.

**Update (Phase 9.7.2.3):** parts of this assessment were superseded by Phase 9.7.2.x. The general CloudFormation execution policy no longer contains CloudFront or site-bucket permissions, and the frontend stack is created in a **supervised administrator operation**, not as a normal deployment step (the identity that carries its temporary permissions is an open administrator decision: `docs/phase-9.7.2-administrator-preparation.md`, sections 14.1 and 14.1a). The three stateful stacks (data lake, registry, run history) and the frontend stack are not covered by the routine rollback permissions; deleting them needs separate approval (section 9.1 of the same document). Where this report says otherwise (sections 4.2 and 13), the Phase 9.7.2.x documents prevail.

Terms used for severity: **Critical** blocks safe deployment; **High** should be resolved before deployment; **Medium** before
production use; **Low** is an improvement or a documentation issue.

## 1. Executive summary

The repository is in good shape locally. All 1,070 backend and infrastructure tests pass, the frontend builds, and all
synthesis configurations succeed with no dependency cycle. The design is conservative: no wildcard actions anywhere, one
unavoidable wildcard resource (`ecr:GetAuthorizationToken`), prefix-scoped S3, one-table DynamoDB access, no NAT gateway, and
execution disabled in code, configuration and IAM-adjacent behaviour.

It is **not ready to deploy yet**, for reasons that are about the real account rather than the code:

1. There is no verified deployment identity, and the state of CDK bootstrap in the account (including whether the game's
   `CDKToolkit` stack is shared) is unknown.
2. The API certificate that was supplied has not been checked (coverage, region, validation), the frontend certificate does
   not exist, and no DNS validation has been done.
3. The Cognito user pool and app client are not created by this repository and are unverified.
4. Several runtime behaviours cannot be proven locally: whether ECS tasks can reach Cognito, ECR and CloudWatch with public
   IPs and no NAT; whether Lambda accepts the account-conditioned trust policy used on every Lambda execution role; whether
   Step Functions sees the Lambda error name the retry expects; whether Glue and Lake Formation allow partition registration.

**Conclusion: READY AFTER SPECIFIED BLOCKERS ARE RESOLVED** (section 14).

## 2. Overall readiness assessment

| Area | Locally verified | AWS-verified | Assessment |
|---|---|---|---|
| Application code, API, auth, run store | yes (tests, doubles) | no | good; JWKS reachability unproven |
| Infrastructure code | yes (synthesis and template tests) | no | good; deploy order and permissions unproven |
| IAM design | yes (template review) | no | least-privilege on paper; Lambda trust condition unproven |
| Pipeline and workflow | yes (simulator, doubles) | no | good; real error names, Glue and timing unproven |
| Reconciliation | yes (doubles) | no | good; not scheduled or wired at start-up |
| Network and HTTPS | partly (template review) | no | acceptable design; connectivity unproven |
| Cognito | frontend and backend config tests | no | external prerequisite, not created |
| Operations (alarms, budget, logs) | n/a | no | minimal; proposals only |

## 3. Resource inventory

Classification: **D** defined in CDK; **I** imported or referenced existing; **F** created only when a flag or context is
supplied; **N** not implemented; **X** external prerequisite.

### 3.1 CDK stacks and dependencies

| Stack | Needs | Created when | Account pinned |
|---|---|---|---|
| `IrishEnergyDataLakeStack` | none | always | **no** (account comes from the CLI credentials) |
| `IrishEnergyApiRegistryStack` | none | always | yes |
| `IrishEnergyRunHistoryStack` | none | always | yes |
| `IrishEnergyPipelineStack` | none (image asset needs bootstrap) | always | yes |
| `IrishEnergyWorkflowStack` | Pipeline, RunHistory | always | yes |
| `IrishEnergyReconcilerStack` | RunHistory | only with `-c reconcilerEnabled=true` | yes |
| `IrishEnergyApiEdgeStack` | Workflow, RunHistory, ApiRegistry (service only) | only with both certificate ARNs; the ECS service only with `apiImageTag`, `cognitoUserPoolId` and `cognitoAppClientId` as well | yes |
| `IrishEnergyFrontendStack` | none | only with both certificate ARNs | yes |

No dependency cycle exists (synthesis succeeds in every configuration). Cross-stack coupling uses CloudFormation exports
(`IrishEnergyPipeline-*FunctionArn`, `IrishEnergyPipelineWorkflow-StateMachineArn`, and generated exports of the table and the
ECR repository), so exports cannot be removed while another stack imports them (see the rollback plan). The workflow stack
imports by export name, so deploy order matters. The Glue table that the Glue-registration function needs lives in the data
lake stack with no CloudFormation dependency on the pipeline; ordering is by procedure.

### 3.2 Resources

| Resource | Stack | Class | Notes |
|---|---|---|---|
| CDK bootstrap (`CDKToolkit`, asset bucket, asset ECR repository, deploy and publishing roles) | n/a | X | state unknown; may be shared with the game |
| Route 53 hosted zone `ai-autofy-general-server.com` | n/a | X | existing; records are manual |
| DNS validation records, `energy-api.` and `energy.` alias records | n/a | N / X | manual by design |
| ACM certificate for `energy-api.` (eu-west-1) | referenced by ARN | I / X | supplied ARN unverified |
| ACM certificate for `energy.` (us-east-1) | referenced by ARN | X | does not exist yet |
| Cognito user pool, app client, hosted UI domain, `viewer` and `operator` groups | referenced by IDs | X | not defined in CDK |
| S3 data lake bucket `irish-energy-data-platform-851725545966` | DataLake and others | I | imported by name; encryption and versioning unknown |
| Glue database `irish_energy_analytics`, table `daily_electricity_demand`, Athena workgroup `irish-energy-analytics` | DataLake | D | may already exist (manual or earlier deploy): unknown |
| ECR repository `irish-energy-api` (immutable tags, scan on push, lifecycle, retained) | ApiRegistry | D | image push is manual |
| Lambda image asset (shared by four functions) | Pipeline | D | published to the bootstrap asset repository at deploy |
| DynamoDB `irish-energy-run-history` (on-demand, `gsi1`, TTL `expires_at`, PITR, deletion protection, retained) | RunHistory | D | fixed name |
| Four pipeline Lambda functions, four execution roles, four log groups (30 days) | Pipeline | D | not in a VPC |
| Step Functions Standard `irish-energy-pipeline` and its role | Workflow | D | no logging, no tracing |
| Reconciler function (zip), role, log group, scheduler role | Reconciler | F | |
| EventBridge Scheduler schedule `irish-energy-run-reconciler` | Reconciler | F | `DISABLED` unless `-c reconcilerScheduleState=ENABLED` |
| VPC, two public subnets, internet gateway, route tables, two security groups | ApiEdge | D | no NAT, no private subnets |
| S3 and DynamoDB gateway endpoints | ApiEdge | F | only with the ECS service |
| Application Load Balancer (internet-facing, deletion protection), port 80 redirect, HTTPS listener, default 404, host rule | ApiEdge | D | 503 until the service exists |
| Target group, ECS cluster, Fargate task definition (0.25 vCPU, 1 GB), service, execution role, task role, log group (retained) | ApiEdge | F | needs image tag and Cognito IDs |
| Secrets Manager secrets | ApiEdge | N | optional per configuration, none defined |
| Frontend S3 bucket (private, SSL only, retained), origin access control, CloudFront distribution, response headers policy | Frontend | D | needs both certificate ARNs |
| Frontend build upload and cache invalidation | n/a | N | no automation |
| Alarms, dashboard, budget, WAF, ALB access logs, CloudFront logs, Step Functions logging, X-Ray | n/a | N | proposals only |
| NAT gateway, interface endpoints | n/a | N | deliberately absent |
| The Bluff or Believe game's resources | n/a | not touched | `api.` and `play.` hostnames are refused by the code |

Missing outputs (Low): the pipeline stack exports no log group names, the reconciler stack exports no schedule name, and the
workflow stack exports only the state machine ARN. Environment problem (High): the data lake stack is not account-pinned (see
finding H-7).

## 4. IAM permissions matrix

Deployment-time identities are **not defined in this repository**; the entries below say what the CDK code implies and must be
confirmed by an administrator, with a later read-only permissions audit or the IAM policy simulator. No permission was checked
against AWS in this phase. Do not use `AdministratorAccess`; the CloudFormation execution role should be a custom policy.

### 4.1 Runtime identities (defined in CDK; the statements are what the templates contain)

| Identity | Actions and resource scope | Wildcards | Implemented in CDK | Unverified | When |
|---|---|---|---|---|---|
| ECS task execution role | `ecr:BatchCheckLayerAvailability`, `GetDownloadUrlForLayer`, `BatchGetImage` on the one repository; `logs:CreateLogStream`, `PutLogEvents` on its log group; `ecr:GetAuthorizationToken` | `ecr:GetAuthorizationToken` on `*` (AWS requires it) | yes | trust with `aws:SourceAccount` | runtime |
| ECS application task role | `s3:ListBucket` on the bucket (prefix `gold/eirgrid/daily_demand/*`); `s3:GetObject` on that prefix; `dynamodb:GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` on the table; `dynamodb:Query` on `gsi1`; `states:StartExecution` on the one state machine; `states:DescribeExecution` on its executions | none | yes | actions work from Fargate; trust condition | runtime (the `states:` pair is dormant) |
| Step Functions execution role | `dynamodb:UpdateItem`, `DeleteItem` on the table; `lambda:InvokeFunction` on the four function ARNs | none | yes | unqualified ARN is enough for `lambda:invoke` (per AWS documentation) | runtime |
| Bronze Lambda role | `s3:PutObject` on `bronze/eirgrid/demand_actual/*`; own log group | none | yes | trust condition | runtime |
| Silver Lambda role | `s3:GetObject` Bronze prefix; `s3:PutObject` Silver prefix; own log group | none | yes | trust condition | runtime |
| Gold Lambda role | `s3:GetObject` Silver prefix; `s3:PutObject` Gold prefix; own log group | none | yes | trust condition | runtime |
| Glue-registration Lambda role | `glue:GetTable`, `glue:CreatePartition` on the catalog, database and table; own log group | none | yes | Lake Formation settings; trust condition | runtime |
| Reconciler Lambda role (optional) | `states:DescribeExecution` on the state machine's executions; `dynamodb:GetItem`, `UpdateItem`, `DeleteItem` on the table; `dynamodb:Query` on `gsi1`; own log group | none | yes (flag) | trust condition | runtime |
| EventBridge Scheduler invocation role (optional) | `lambda:InvokeFunction` on the reconciler function | none | yes (flag) | trust condition | runtime |
| DynamoDB gateway endpoint policy | the same four item actions on the table and `Query` on the index | principal `*` (required in endpoint policies) | yes | n/a | runtime |
| CloudFront site bucket policy | deny non-TLS access; allow `s3:GetObject` to the CloudFront service principal for this account's distribution (origin access control) | principal `*` only in the deny | yes | n/a | runtime |

### 4.2 Deployment-time identities (not defined here; to be confirmed)

| Identity | What is needed (service level) | Scope and unavoidable wildcards | In CDK | Status |
|---|---|---|---|---|
| Restricted developer identity (`irish-energy-data-platform-dev`) | read the Gold prefix; run the API and read-only smoke tests | none beyond Gold read; must **not** be widened | n/a | Gold read confirmed once; lake-layer writes reported; Glue and Athena reported denied; everything else unverified |
| CDK deployment identity (administrator-controlled) | `sts:AssumeRole` on the bootstrap deploy, file-publishing and image-publishing roles (`arn:aws:iam::851725545966:role/cdk-<qualifier>-*`), `cloudformation:DescribeStacks` and related reads; a Docker daemon and permission to push to the image-publishing role | role-name pattern wildcard | no | unverified; depends on whether bootstrap exists and which qualifier it uses |
| CloudFormation execution role (bootstrap default is broad) | create and manage only: DynamoDB table; Lambda; IAM roles and `iam:PassRole` for the energy roles; Step Functions; EventBridge Scheduler; ECR repository; ECS; EC2 (VPC, subnets, security groups, gateway endpoints); Elastic Load Balancing; CloudWatch Logs; Glue and Athena (no delete). **CloudFront and the S3 site bucket were removed from this role in Phase 9.7.2.2** | resource-level scoping to `irish-energy-*` names and `arn:aws:iam::851725545966:role/IrishEnergy*` where the service supports it; unavoidable `*`: EC2 networking creation actions (tag-conditioned), `ecr:GetAuthorizationToken`, `elasticloadbalancing:Create*` partly | no | must be a custom execution policy (`--cloudformation-execution-policies`) with a permissions boundary on created roles, agreed with an administrator; unverified |
| ECR image publishing for the API | `ecr:GetAuthorizationToken` plus push actions on `irish-energy-api` only | token action on `*` | no (manual push) | unverified |
| ACM and Route 53 | `acm:RequestCertificate`, `DescribeCertificate` in two regions; `route53:ListHostedZones`, `ChangeResourceRecordSets` on the one hosted zone | `ListHostedZones` on `*` | no | unverified; manual by design |
| Cognito administration | create and update the user pool, app client, domain and groups | the pool ARN once it exists | no | unverified |
| Frontend publishing | `s3:PutObject`, `DeleteObject`, `ListBucket` on the site bucket; `cloudfront:CreateInvalidation` on the distribution | none | no | unverified; no automation exists |

`iam:PassRole` is needed by the deployment path for the ECS task and execution roles (service `ecs-tasks.amazonaws.com`), the
Lambda execution roles (`lambda.amazonaws.com`), the state machine role (`states.amazonaws.com`) and the scheduler role
(`scheduler.amazonaws.com`); scope it to the `IrishEnergy*` role names with `iam:PassedToService`. None of this is implemented
in the repository; it is a specification for the administrator.

## 5. IAM least-privilege security review

Reviewed from the synthesised templates of every stack, with the optional stacks enabled.

- **Action `*`:** none anywhere. **Resource `*`:** exactly one, `ecr:GetAuthorizationToken` on the ECS task execution role,
  which AWS requires.
- **Trust policies:** all nine roles (two ECS, four pipeline Lambda, the state machine, and the optional reconciler and
  scheduler roles) trust only one service principal (`ecs-tasks`, `lambda`, `states` or `scheduler`) and add a
  `StringEquals aws:SourceAccount` condition for this account. No role trusts an account, user or `*`.
- **API role and pipeline Lambdas:** confirmed the API role has no `lambda:` action of any kind (it can start and describe one
  state machine only, and only dormantly). Confirmed the reconciler role has no `StartExecution`, `PutItem`, transactions,
  Scan or any non-listed service. Confirmed the four pipeline roles have only S3 object and Glue partition permissions and
  their own log streams; none can reach DynamoDB, Step Functions, Athena, Cognito, IAM or EC2.
- **DynamoDB:** table-scoped item actions, `Query` on `gsi1` only; no Scan, batch, transaction or table-management action.
- **S3:** every object statement is a layer prefix; the only bucket-level statement is `s3:ListBucket` with a prefix
  condition on the API role. No pipeline role can list the bucket.
- **PassRole:** no role in the templates has `iam:PassRole` or any IAM action; the deployment path needs it (section 4.2).
- **Managed policies:** none attached to any role (no `AWSLambdaBasicExecutionRole`); logging permissions are exact.

A test (`tests/test_pipeline_security_regression.py`) enforces these properties over the real synthesised templates.

Proposed changes (not applied): see findings H-3 (verify or remove the Lambda trust condition), M-2 (add alarms, which need no
new runtime permission) and the custom CloudFormation execution policy in 4.2.

## 6. Security findings

| ID | Severity | Finding | Recommended action |
|---|---|---|---|
| C-1 | Critical | No verified deployment identity or permissions audit; CDK bootstrap state unknown, and a shared `CDKToolkit` with the game is possible. A wrong bootstrap or an `--all` deploy could affect the game's stack | administrator confirms identity, custom CloudFormation execution policy and bootstrap state read-only before anything else; deploy named stacks only |
| C-2 | Critical | The supplied API certificate (`.../97548b1e-afa4-42c3-8cf0-a0fa57e3edd9`) is unverified (hostname coverage, validation status); the `us-east-1` frontend certificate does not exist; no DNS validation done | see section 8; both must be `Issued`, cover only the energy hostnames, and be in the right regions |
| C-3 | Critical | The Cognito user pool, app client, hosted UI domain and groups are not created by this repository and are unverified; the ECS service cannot be synthesised without their IDs | create and verify per section 9 before the ECS service |
| H-1 | High | A plaintext AWS access key file (`aws-access-key.txt`) sits in the project directory (git-ignored, so not committed; its contents were not read) | owner removes it and rotates that key; use a profile or SSO |
| H-2 | High | ECS tasks use public IPs and no NAT, so Cognito JWKS, ECR, CloudWatch and Step Functions calls rely on a plain internet path and the task security group's HTTPS egress. JWKS is fetched lazily on the first authenticated request, so the health check cannot detect a broken path | verify with a read-only authenticated smoke test; keep the NAT decision open (cost, section 11) |
| H-3 | High | Every Lambda execution role trusts `lambda.amazonaws.com` with an `aws:SourceAccount` condition. The Lambda documentation page reviewed shows only the plain service-principal trust; whether Lambda supplies that key when assuming the role is **unverified**. If it does not, every function would fail at its first invocation (not at deployment) | verify with a supervised invocation, or remove the condition (proposed change, not applied; see `docs/phase-9.7.1-local-corrections.md`). The same condition on ECS, Step Functions and Scheduler roles follows documented confused-deputy guidance but is also unverified in this account |
| H-4 | High | Image publication is unverified: the pipeline image is a CDK asset built with Docker at deploy time on an amd64-compatible machine and published through bootstrap roles; Lambda pulling from the bootstrap repository depends on its repository policy; the API image must be built, scanned and pushed to `irish-energy-api` before the ECS service is created (tags are immutable) | administrator confirms Docker availability and the bootstrap repository policy; publish the API image before the edge stack |
| H-5 | High | Glue registration is unverified: the developer identity was denied Glue; the function role has the IAM statements, but Lake Formation settings (if enabled) can still deny it; the Glue table must already exist | administrator confirms Glue and Lake Formation; deploy the data lake stack first |
| H-6 | High | Frontend delivery is not automated (no upload, no invalidation) and the Vite build needs `VITE_API_BASE_URL` and the Cognito variables at build time (the code default is `http://127.0.0.1:8001`) | define a manual, reviewed publishing procedure with the permissions in 4.2 |
| H-7 | High | `IrishEnergyDataLakeStack` is not account-pinned; its account comes from the active credentials, unlike every other stack, and its Glue and Athena resources may already exist (conflict) | confirm existing resources first; consider pinning the environment (proposal) | **pinned in Phase 9.7.1**; existing-resource check still open |
| H-8 | High | Exports couple the stacks: a stack that exports a value cannot be updated to remove it while another imports it, and deletion must follow the reverse order | follow the rollback order in section 13 |
| M-1 | Medium | The CloudFront response headers policy sets HSTS, content-type options, frame options and referrer policy but **no Content-Security-Policy**; the SPA keeps tokens in memory, so CSP is a valuable defence | add a reviewed CSP |
| M-2 | Medium | No alarms, budget, dashboard, ALB access logs, WAF, CloudFront logs or Step Functions logging | add the proposals in section 12 and a budget alert before deployment |
| M-3 | Medium | The reconciler is optional, schedule-disabled and not called at API start-up, which still uses lease-only `recover_interrupted` | part of the separate activation change |
| M-4 | Medium | The ingestion retry matches the error name `TransientEirGridError`; documentation supports it, but unhandled Lambda errors were historically reported as `Lambda.Unknown` | confirm with one supervised, injected failure |
| M-5 | Medium | The API rate limiter is in memory per task (one task by default) | shared limiter or WAF before running more than one task |
| M-6 | Medium | Fixed resource names (`irish-energy-*` table, functions, state machine, schedule) and retained resources (table, site bucket, ECR repository, API log group) can collide on redeploy | confirm no existing resource uses the names; plan manual clean-up after any teardown |
| M-7 | Medium | Creating the Scheduler schedule needs `scheduler:CreateSchedule` plus `iam:PassRole` on its role | include in the deployment policy only if the reconciler is approved |
| L-1 | Low | `docs/deployment-dns-and-certificates.md` section 4 still says synthesis without certificates produces only the data lake stack (it now produces five) | **resolved in Phase 9.7.1** |
| L-2 | Low | The ALB listens on port 80 from anywhere (redirect only); the API adds no security headers (JSON only; docs endpoints are disabled in production); CORS allows `GET`/`POST` for one origin without credentials | acceptable |
| L-3 | Low | Rate-limit and CORS values rest on a single frontend origin; the local development origin needs its own Cognito client | document, no change |

## 7. Network and HTTPS findings

- **Topology.** One VPC, two public subnets in two availability zones, an internet gateway, no private subnets, no NAT. The ALB
  is internet-facing. Tasks have public IPs (`assign_public_ip=True`) so they can reach AWS APIs, ECR and Cognito without NAT.
  This is the cheapest viable design but means each task holds a public IPv4 address.
- **Security groups.** ALB: ingress 443 and 80 from anywhere, egress only to the task port (added by the target registration).
  Task: ingress only from the ALB on the container port, egress only TCP 443 to anywhere. So DNS (VPC resolver) works,
  HTTPS reaches Cognito, ECR, CloudWatch, Step Functions and (through the gateway endpoints) S3 and DynamoDB. Nothing else
  leaves the task.
- **Gateway endpoints.** S3 (unrestricted default policy) and DynamoDB (table and index only). Free, only with the service.
  There are no interface endpoints for ECR, CloudWatch or Step Functions, so those use the internet path.
- **Cognito JWKS.** Fetched from `https://cognito-idp.<region>.amazonaws.com/<pool>/.well-known/jwks.json` (HTTPS only,
  5-second timeout, 1 MB limit, cached for an hour, refetch cooldown of one minute). Reachability from a Fargate task with a
  public IP and 443 egress is expected but **not verified**; a failure surfaces as 503 on the first authenticated request, not
  as a failed health check. Local authentication tests use a JWKS stub and prove nothing about the network.
- **HTTPS.** ALB: port 80 permanently redirects to 443; HTTPS listener with the `RECOMMENDED_TLS` policy; invalid headers are
  dropped; deletion protection on. CloudFront: HTTPS redirect, `TLS_V1_2_2021`, origin access control to a private,
  SSL-only, S3-managed-encryption bucket, `index.html` fallback for 403 and 404 (single-page application).
- **Certificates.** The ALB needs an `eu-west-1` certificate; CloudFront needs a `us-east-1` certificate. The code rejects
  certificates in the wrong region or account, but it cannot check hostname coverage or status.
- **Hostname safety.** The configuration rejects `api.` and `play.` (the game), the apex, `www.`, wildcards and any other
  account. The energy hostnames are fixed to `energy-api.` and `energy.`; the energy stacks never reference the game's
  certificate, ALB or distribution. The code has no Route 53 resources, so it cannot change DNS.
- **Application.** JWT verification: RS256 Cognito access tokens, issuer, `token_use=access`, `client_id` and expiry checked,
  tokens only from the `Authorization` header; CORS limited to the configured origin; rate limiting and sanitised errors are
  covered by tests; logs are JSON with redaction of bearer tokens, JWTs, key IDs and secrets.

## 8. ACM and DNS prerequisites

1. **API certificate (`eu-west-1`).** The supplied certificate must be inspected by an administrator: status `Issued`, region
   `eu-west-1`, and the subject or alternative names include `energy-api.ai-autofy-general-server.com` (and ideally only
   that). Do not reuse the game's certificate. Coverage and validation status are **unverified**.
2. **Frontend certificate (`us-east-1`).** Does not exist. Request a public certificate in `us-east-1` for exactly
   `energy.ai-autofy-general-server.com` with DNS validation.
3. **DNS validation.** Each certificate shows one CNAME; add them to the existing public hosted zone, new records only,
   after checking the names are free. They are unique per certificate and must not touch the game's records.
4. **Alias records** (after the stacks exist): `energy-api.` A alias to the ALB (`AlbDnsName`, `AlbCanonicalHostedZoneId`),
   and `energy.` A and AAAA aliases to the distribution (hosted zone ID `Z2FDTNDATAQYW2`).
5. Nothing in this phase created or changed a certificate or DNS record.

## 9. Cognito prerequisites

Created outside this repository (and unverified):

- A user pool in `eu-west-1` (the API's expected region) with groups `viewer` and `operator` (the API reads the groups claim;
  operators can only reach the disabled `POST /api/runs`).
- A **public** app client with **no secret**, OAuth authorization-code grant with PKCE (`responseType: code`), scope `openid`
  (the default; any more must be listed in `VITE_COGNITO_SCOPES`), access tokens carrying `client_id`.
- Callback URL and sign-out URL exactly `https://energy.ai-autofy-general-server.com/` (the frontend validates the redirect
  values). Use a separate client for local development (for example `http://localhost:5173/`), never add localhost to the
  production client.
- A hosted UI domain (a Cognito prefix domain needs no certificate; a custom domain would need another `us-east-1`
  certificate and DNS, which is not planned).
- Frontend build variables: `VITE_API_BASE_URL=https://energy-api.ai-autofy-general-server.com`, `VITE_COGNITO_USER_POOL_ID`,
  `VITE_COGNITO_APP_CLIENT_ID`, `VITE_COGNITO_DOMAIN`, `VITE_COGNITO_REDIRECT_SIGN_IN`, `VITE_COGNITO_REDIRECT_SIGN_OUT`.
  Tokens are held in memory (not in browser storage).
- API environment (set by the ECS stack from CDK context, no secrets): `API_ENV=production`, `COGNITO_REGION`,
  `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `CORS_ALLOWED_ORIGINS=https://energy.ai-autofy-general-server.com`,
  `RUN_HISTORY_BACKEND=dynamodb`, `RUN_HISTORY_TABLE`, `RUN_HISTORY_REGION`, `S3_BUCKET`, `PIPELINE_STATE_MACHINE_ARN`
  (dormant). Production start-up refuses to run without Cognito and DynamoDB settings, and refuses
  `PIPELINE_EXECUTION_ENABLED=true`.

## 10. Pipeline and reconciliation readiness

| Behaviour | Locally verified (doubles, simulator) | Needs real AWS verification |
|---|---|---|
| EirGrid ingestion; transient (429, 5xx, timeouts) vs permanent classification and bounded 10/20/40 s retry | yes | real source availability, rate limits and terms of use; real Lambda error names (M-4) |
| Run-specific Bronze and Silver names; retries rewrite the same object; concurrent runs do not collide | yes | none beyond S3 semantics |
| Gold: one canonical object per run date, latest successful run replaces it; Glue layout and analytics reader unchanged | yes | Glue partition registration, Lake Formation (H-5) |
| DynamoDB lock, lease (45 min), idempotency, run record transitions | yes (in-memory double) | real conditional-write and transaction behaviour, throttling |
| Step Functions: stage order, `ResultPath` wiring, retries, catches, failure marking (including `States.Runtime`) | yes (local interpreter of the synthesised definition) | the interpreter is not AWS; real state transitions, timeouts, payload size |
| Workflow 30-minute limit against the 45-minute lease | yes (constants and tests) | real execution timing, cold starts of an 834 MB image |
| Lambda sizing: bronze 256 MiB / 120 s, silver and gold 1024 MiB / 300 s, glue 512 MiB / 60 s (estimates) | no | first supervised run must measure memory and duration |
| Reconciler: statuses, lock release, lease renewal, missing and unqueryable executions, expired lease behind a live execution | yes | `DescribeExecution` behaviour, rates and permissions; Scheduler delivery |
| Failed lock release and recovery after API restart | yes | real DynamoDB failure modes |

Remaining design limits (documented earlier, unchanged): a partial failure can lack a `gold_key`; Bronze and Silver objects
from failed attempts are never cleaned up; callers that use the store directly bypass the expired-lock guard; the API does
not yet call the reconciler at start-up. The reconciler schedule stays disabled by default and the pipeline stays disabled
throughout this plan.

## 11. Cost estimate (euros per month, `eu-west-1`, low usage)

**All figures are estimates from remembered public prices, converted at about EUR 0.92 per USD (the rate used in earlier
documents); none was verified, and AWS prices must be re-checked before relying on them.** They assume the API task runs 24
hours a day, one daily pipeline run at most, and negligible traffic.

| Item | Basis (assumed) | Fixed or usage | Likely EUR/month |
|---|---|---|---|
| Application Load Balancer | about USD 0.025 per hour plus a small LCU charge | fixed | 17 to 22 |
| Public IPv4 addresses | 3 (ALB in two zones, one task) at about USD 0.005 per hour each | fixed | 10 |
| ECS Fargate API task | 0.25 vCPU, 1 GB, Linux x86, 24/7 | fixed while running | 10 to 11 |
| Pipeline Lambdas | a few seconds a day; far inside the free tier | usage | under 0.1 |
| Reconciler Lambda | only if the schedule is enabled: about 8,640 short runs a month, inside the free tier | usage | 0 to 0.1 |
| EventBridge Scheduler | about 8,640 invocations, inside the free tier | usage | 0 |
| Step Functions Standard | about 20 transitions per run, inside the 4,000 free transitions | usage | under 0.05 |
| DynamoDB on demand and point-in-time recovery | a few hundred requests a day, tiny storage | usage | 0.05 to 0.5 |
| S3 (site bucket, lake layers) and requests | megabytes | usage | 0.05 to 0.5 |
| CloudFront | inside the free allowance at this traffic | usage | 0 |
| ECR storage | API image plus the CDK asset repository (compressed images of a few hundred MB, up to 30 kept) | usage | 0.1 to 1.5 |
| CloudWatch Logs | tens of MB ingested, 30-day retention | usage | 0.1 to 1 |
| CloudWatch custom metrics | the reconciler emits 10 dimensionless metrics, at the free allowance of 10; each extra is about USD 0.30 | usage | 0 to 3 |
| EventBridge, Athena, Route 53 queries | negligible; the Athena workgroup stops queries at 10 MB scanned | usage | 0 to 0.5 |
| Data transfer out | small API responses, small Cognito and AWS API traffic | usage | 0.1 to 1 |
| NAT gateway | **not used** | n/a | 0 (adds about 33 to 40 if ever added) |

1. **Estimated fixed monthly cost:** about EUR 37 to 43 (load balancer, three public IPv4 addresses, the running task).
2. **Usage-dependent costs:** about EUR 0.5 to 8 depending on logging, metrics, ECR retention and traffic.
3. **Likely minimum monthly cost:** about EUR 38 to 40, with one task running continuously and almost no traffic.
4. **Conservative monthly estimate:** about EUR 50 to 60 (higher load balancer charge, logs and metrics, ECR growth,
   some data transfer, a daily pipeline run, a second task during deployments).
5. **Potential unexpected cost drivers:** adding a NAT gateway (about EUR 33 to 40); more tasks (each adds a public IPv4
   address and Fargate hours); internet scanner traffic raising load balancer capacity units; debug logging or very chatty
   logs; custom metrics beyond ten; the asset repository accumulating images; CloudFront invalidations beyond the free
   allowance; Athena scans without the workgroup cutoff; the load balancer and its addresses continuing to cost while the ECS
   service is stopped (stopping the service saves only the task and its address); PITR storage growth. A budget alert should
   be created before the first deployment.

## 12. Operational monitoring proposals (not created)

Proposed alarms that need no new runtime permission: `StaleActiveRuns` greater than zero for two periods; `ReconcileFailures`
or `ReconcileErrors` above zero, and missing data on `RunsChecked`; `LocksReleased` above zero; Step Functions
`ExecutionsFailed`, `ExecutionsTimedOut` and `ExecutionsAborted`; errors on the Glue-registration and bronze functions; ALB
`HTTPCode_Target_5XX_Count` and `UnHealthyHostCount`; ECS service running task count below one; a monthly cost budget. Add
Step Functions logging and ALB access logs only after reviewing their permissions and cost.

## 13. Deployment sequence and rollback plan

The pipeline stays disabled throughout. Every step below needs explicit approval and is not performed by this phase. The
order differs from the draft in three places because of dependencies: the Cognito pool must exist **before** the ECS service
(its IDs are required inputs), the API image must be pushed **before** the edge stack that creates the service, and the budget
alert comes first. Deploy named stacks one at a time and review `cdk diff` before each; never `--all`.

| # | Stage | Prerequisites | Expected resources | Permissions | Verification | Rollback | Cost |
|---|---|---|---|---|---|---|---|
| 0 | Budget alert, administrator permission review, read-only check of existing bootstrap and of the game's stacks | owner approval | none | administrator (read-only audit) | bootstrap qualifier and version known; game untouched; custom CloudFormation execution policy agreed | none needed | no |
| 1 | CDK bootstrap, only if needed and only after step 0 | step 0 | bootstrap stack, asset bucket and repository, roles | administrator | bootstrap stack healthy; qualifier recorded | delete only a stack this project created; never the game's | small |
| 2 | ACM certificates and DNS validation (two regions) | hosted zone access | two certificates, two validation records | ACM, Route 53 (new records only) | both `Issued`, correct regions, only energy names | delete the new certificates and validation records | no |
| 3 | Cognito user pool, groups, public client, hosted UI domain | callback URL decided | pool and client | Cognito administration | values recorded; client has no secret, PKCE code flow | delete the pool and client (no stack involved) | no (free tier) |
| 4 | Data lake: confirm the bucket, Glue database and table, Athena workgroup, Lake Formation; deploy `IrishEnergyDataLakeStack` only if they do not already exist | read-only audit | Glue database and table, Athena workgroup | Glue, Athena, S3 | Glue table present with the expected schema; bucket encryption known | stack delete (the bucket is imported, not owned) | negligible |
| 5 | `IrishEnergyApiRegistryStack`, then build, scan and push the API image | bootstrap, Docker, image tag chosen | ECR repository, image | ECR | image present with the chosen immutable tag | delete the image tag (immutable: use a new tag); repository is retained | negligible |
| 6 | `IrishEnergyRunHistoryStack` | none | DynamoDB table | DynamoDB | table active, point-in-time recovery on, deletion protection on | table is retained and protected: disable protection and delete manually only if intended | negligible |
| 7 | `IrishEnergyPipelineStack` | step 1, Docker | four Lambda functions, roles, log groups, published image asset | Lambda, IAM (`PassRole`), ECR, Logs | functions `Active`; **no invocation** | stack delete (reverse order: after the workflow stack) | negligible |
| 8 | `IrishEnergyWorkflowStack` | steps 6 and 7 | state machine and role | Step Functions, IAM | state machine exists; **no execution** | stack delete before the pipeline stack | no |
| 9 | Optional: `IrishEnergyReconcilerStack` with `-c reconcilerEnabled=true`, schedule left `DISABLED` | step 6 | function, roles, schedule (disabled) | Lambda, Scheduler, IAM | schedule state `DISABLED`; no invocation | stack delete | negligible |
| 10 | `IrishEnergyApiEdgeStack` with certificates, image tag and Cognito IDs (VPC, ALB, ECS service, endpoints) | steps 2, 3, 5, 6, 8 | VPC, ALB, listeners, ECS service, roles, gateway endpoints | EC2, ELB, ECS, IAM (`PassRole`), Logs | service reaches steady state; target healthy; execution still disabled in the environment | disable ALB deletion protection, then stack delete; the log group is retained | **yes** (about EUR 38 per month) |
| 11 | **Supervised administrator operation** (not a normal deployment step and not performed by the general execution role): `IrishEnergyFrontendStack`, then build with the production `VITE_*` values and upload | steps 2, 3, 10 and an administrator decision on which identity carries the temporary permissions | bucket, distribution, headers policy, uploaded site | CloudFront and S3 permissions outside the general package, temporary and separately approved | site loads over HTTPS from the distribution domain | delete the distribution first, then empty and delete the retained bucket, in a supervised session | small |
| 12 | DNS alias records (`energy-api.`, `energy.`) | steps 10 and 11 | two alias records | Route 53 (new records) | names resolve to the energy ALB and distribution only | delete the two records | negligible |
| 13 | HTTPS and authentication verification | step 12 | none | none | valid certificates, HTTP redirects to HTTPS, anonymous API call returns 401, a real Cognito sign-in obtains a token the API accepts (this also proves JWKS reachability) | n/a | no |
| 14 | Read-only API and analytics smoke tests | step 13 | none | developer identity (Gold read) | `GET /health`, `GET /api/runs`, analytics endpoint; `POST /api/runs` returns 503 | n/a | no |
| 15 | Supervised pipeline verification | **separate explicit approval and a separate activation phase** | n/a | n/a | n/a | n/a | n/a |

General rollback rules: stacks roll back automatically on a failed create; delete in reverse dependency order (ApiEdge,
Reconciler and Workflow, then Pipeline, then RunHistory, with Registry and Frontend independent) because of the exports. The RunHistory, Registry and DataLake stacks and the Frontend stack are not deleted with the routine rollback permissions: deleting them needs separate approval, and ApiEdge teardown can stop at the ECS task definition (Phase 9.7.2 document, section 9). Retained resources (table, ECR repository, site bucket, API log group) must be removed by hand and will block a clean redeploy
with the same names; remove DNS records first on any teardown; none of it touches the game.

## 14. Unresolved decisions and actions requiring administrator approval

Decisions for the owner: the deployment identity model and custom CloudFormation execution policy; whether the account's
bootstrap is shared with the game and what to do about it; which API certificate to use; the Cognito domain style and group
membership; whether to pin the data lake stack's account; whether to keep tasks on public IPs (cheapest) or add NAT
(about EUR 33 to 40 more); whether to add CSP, WAF and logging; when, and whether, to enable the reconciler schedule; the
alarm and budget limits.

Administrator approvals required before any deployment: confirm or create the deployment role and execution policy; read-only
audit of CDK bootstrap and of the game's stacks; certificate issuance and DNS validation records; Cognito creation; Glue and
Lake Formation confirmation and permission to deploy the data lake stack; ECR image push; stack deployments in the order above;
frontend publishing permissions; alias records; removal and rotation of the plaintext key file (H-1); and, separately and
later, any activation of the pipeline.

## 15. Locally tested versus unverified on AWS

Locally tested (test doubles, simulators or template inspection): the whole backend and frontend, the API and authentication
(with a JWKS stub), the run store, executor and reconciler (in-memory DynamoDB double and a Step Functions client double),
the stage handlers and processing code (file-backed S3 double, in-memory Glue double, scripted HTTP responses), the state
machine definition (a local interpreter, not AWS), every stack's synthesised IAM, network and configuration, the container
builds and imports (local Docker, invalid inputs only), and hostname and region guards.

Not verified on AWS: every IAM allow and deny, the Lambda trust condition, CDK bootstrap and publishing, certificates and DNS,
Cognito, connectivity from Fargate to Cognito, ECR and CloudWatch, Lambda error names, Step Functions timing and limits,
Glue and Lake Formation, EventBridge Scheduler, DynamoDB throttling, image sizes in ECR, costs, and real EirGrid behaviour.

## 16. Validation performed in this phase

Backend and infrastructure tests, frontend tests, build and lint, all synthesis configurations (including the optional
reconciler) and `git diff --check` were run locally; results are in the Phase 9.7 report that accompanies this document and are
repeated in `docs/production-readiness-review.md`.
