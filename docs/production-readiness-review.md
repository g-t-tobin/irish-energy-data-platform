# Production readiness review

Date: 2026-10-30. Scope: plan Phases 1-7 reviewed as a whole. Review and
documentation only: nothing was deployed, bootstrapped, created or changed in AWS,
no AWS API call was made, and pipeline execution is still disabled.

Labels used throughout: **[Confirmed]** observed in this repository or in a local
run, **[Reported]** stated by the project owner and not re-checked, **[Assumption]**
believed but unverified, **[Open]** needs an administrator or a decision.

## 1. Verdict

**The architecture is ready for an administrator to review and build out, but not
ready to deploy as it stands.** The application layer (authentication,
authorization, rate limiting, sanitization, run history, execution gate) is
implemented and tested. The infrastructure layer is only partly defined: of the
twelve areas in the brief, the ALB/VPC edge, the CloudFront/S3 frontend, the Glue/Athena data lake, the ECR registry, the ECS Fargate API service and the DynamoDB run-history table now exist as synthesizable CDK (none deployed). Step Functions and the pipeline compute exist only as design data, and alarms and Cognito are missing. See sections 4 and 6.

## 2. Validation run for this review

| Check | Result |
|---|---|
| Backend tests (`.venv/bin/python -m pytest -q`) | 545 passed, 1 warning (Starlette `httpx` deprecation) |
| Infrastructure tests (`tests/test_energy_edge_infrastructure.py`, `tests/test_infrastructure_specs.py`) | 47 passed (also included in the 545) |
| Frontend tests (`npm test`) | 73 passed, 5 files |
| Frontend build (`npm run build`) | passed |
| Frontend lint (`npm run lint`) | passed, no warnings |
| Offline CDK synthesis, no certificate ARNs | 1 stack (`IrishEnergyDataLakeStack`); edge stacks reported as skipped |
| Offline CDK synthesis, placeholder certificate ARNs | 3 stacks: `IrishEnergyApiEdgeStack`, `IrishEnergyDataLakeStack`, `IrishEnergyFrontendStack` |

Synthesis ran as `python app.py` with output sent to a temporary directory, so no
credential file was read and no AWS lookup was possible. Nothing in this review was
tested against real AWS, Cognito, DynamoDB or Step Functions.

## 3. Completed components

| Component | State |
|---|---|
| Data pipeline (Bronze, Silver, Gold, Glue registration, exact-key hand-off) | implemented and tested [Confirmed]; one live run reported by the owner [Reported] |
| Gold analytics reader with cache and data-quality fields | implemented and tested |
| Cognito JWT verification (RS256, issuer, `client_id`, access tokens only), viewer/operator roles | implemented and tested with local test keys |
| Rate limiting, request-size limit, sanitized errors, request IDs, redacting JSON logs, security headers | implemented and tested |
| Production startup guard (Cognito settings mandatory in production) | implemented and tested |
| Run history: SQLite (local) and DynamoDB store with lock, lease, idempotency, recovery | implemented; DynamoDB store tested only against an in-memory double; table, permissions and production-only DynamoDB startup rule defined and tested offline (Phase 9.2), not deployed |
| Step Functions executor and reconciler | implemented and tested, **not wired** into the API |
| Execution gate (disabled executor, settings refuse enabling, POST returns 503 for operators) | implemented and tested |
| Frontend sign-in (Amplify, Hosted UI, PKCE, in-memory tokens), GET-only API client, role-based display | implemented and tested with mocks |
| Edge CDK: ALB with HTTPS listener, CloudFront + private S3, hostname and certificate guards | synthesizes and is tested offline |
| API container image and ECS Fargate service, ECR registry, task and execution roles (Phase 9.1) | image built and run locally; CDK synthesizes and is tested offline; not deployed |
| DNS and certificate documentation | written |
| Design data: DynamoDB table, state machine, three IAM policies | written and tested as data only |

## 4. Infrastructure review (what the synthesized stacks actually contain)

| Area | Finding |
|---|---|
| **ECS Fargate API service** | **Defined in CDK (Phase 9.1), not deployed.** Cluster (no Container Insights), Fargate task definition (0.25 vCPU, 1 GB, x86, non-root, read-only root, capabilities dropped, init process, `/tmp` volume), service (1 task, public IP, rolling update with circuit breaker), separate execution and task roles. Included when `apiImageTag` and the Cognito IDs are supplied. The image builds and runs locally. |
| **ALB listener** | Present: 443 HTTPS with the eu-west-1 certificate ARN and a TLS 1.3/1.2 policy; 80 redirects to 443; deletion protection and invalid-header dropping on. Unmatched requests get 404. |
| **Target groups and health checks** | **Defined.** IP target group on port 8000 with `GET /health` (public, expects 200, 30 s interval, 2 healthy / 3 unhealthy), forwarded for the energy host only (priority 10). Without service inputs the host still returns the fixed 503. |
| **VPC and security groups** | Own VPC, 2 public subnets, internet gateway, no NAT. With the service included (Phase 9.1): the ALB may reach the task port only, tasks accept only the ALB and send only HTTPS, and a free S3 gateway endpoint serves both route tables. **Phase 9.3 adds a DynamoDB gateway endpoint** on the same two route tables, with a policy limited to four item actions on the table and `Query` on `gsi1` (a Put-only write transaction is authorised by `PutItem`; there is no `TransactWriteItems` IAM action) (no hourly endpoint charge; other AWS charges still apply). Defined and tested through offline synthesis only: not deployed and never verified against AWS. No flow logs, no interface endpoints. |
| **DynamoDB** | **Defined in CDK (Phase 9.2), not deployed** (`IrishEnergyRunHistoryStack`, built from `infrastructure/run_control/spec.py`): on-demand, keys `pk`/`sk`, index `gsi1` (all attributes), TTL attribute `expires_at`, AWS-managed-key encryption, point-in-time recovery, deletion protection, retained. The TTL applies only to idempotency items. The ECS task role can use exactly this table and index. |
| **Step Functions** | **Defined in CDK (Phase 9.5), not deployed.** A Standard state machine from the Phase 6 generator chains the four Lambda stages with bounded retries on Lambda service errors only, `Catch` on every stage, conditional DynamoDB progress writes, lock release and a 30-minute limit. Synthesised and simulated locally against the real handlers; never executed. No execution logging. |
| **Pipeline compute** | **Defined in CDK (Phase 9.4), not deployed.** The owner chose Lambda container images: four functions (Bronze, Silver, Gold, Glue registration) from one shared x86_64 image, each with its own execution role (S3 prefix-scoped, Glue on one table), 30-day log retention and a 120-300 s timeout (limit 900 s). Handlers wrap the existing, tested stage code. The image is not published and nothing has been invoked; the state machine that chains them is Phase 9.5. |
| **ECR** | **Defined** (`IrishEnergyApiRegistryStack`): `irish-energy-api`, scan on push, AES-256, immutable tags, retained, untagged images removed after 14 days, newest 30 kept. Pipeline image repository still missing. |
| **S3 and CloudFront** | Present: private encrypted bucket (HTTPS-only policy, retained), origin access control, redirect to HTTPS, TLS 1.2 minimum, HSTS and other headers, SPA fallbacks, aliased to `energy.` with the us-east-1 certificate ARN. **Missing:** a way to upload the built site and invalidate the cache, a Content-Security-Policy, access logging. The site loads fonts from Google at runtime, which a strict CSP would block. |
| **CloudWatch** | **API log group defined** (30-day retention, retained). Still missing: alarms, dashboards, ALB access logs, Step Functions logging. |
| **Cognito prerequisites** | **Missing** (not created by design). Needed: user pool, public app client with no secret, Hosted UI domain, callback and sign-out URLs for `https://energy.ai-autofy-general-server.com/`, groups `viewer` and `operator`, short access-token lifetime, MFA for operators. |
| **ACM and DNS** | Documented only. Two separate certificates (eu-west-1 for the API, us-east-1 for CloudFront) and the alias and validation records are manual steps (`docs/deployment-dns-and-certificates.md`). |
| **Data lake** | `IrishEnergyDataLakeStack` (Glue database and table, Athena workgroup) synthesizes. Whether it is deployed is [Reported] as yes by the owner while the README says it is unestablished; the developer identity was denied Glue and Athena access. |

Configuration dependencies still to resolve: API image needs `boto3`, `pandas` and
`pyarrow`, which `requirements-api.txt` does not list; the API must run with
`API_ENV=production`, all three `COGNITO_*` variables, `S3_BUCKET`,
`RUN_HISTORY_BACKEND=dynamodb` and `RUN_HISTORY_TABLE`, and uvicorn with
`--proxy-headers`; the frontend is built with `VITE_*` values at build time.

## 5. IAM security audit

No IAM role or policy exists in any synthesized template. The CDK constructs in use
generate none. All policies below are design documents in
`infrastructure/run_control/spec.py` or a CDK-generated bucket policy. None has been
created, and none was changed.

| # | Policy / used by | Permitted actions and resources | Wildcards | Unnecessary or to remove | Administrator must create |
|---|---|---|---|---|---|
| 1 | **API task role** (ECS task, design) | `s3:ListBucket` on the bucket, only for prefix `gold/eirgrid/daily_demand/*`; `s3:GetObject` on `.../gold/eirgrid/daily_demand/*`; DynamoDB `GetItem`, `PutItem`, `UpdateItem`, `DeleteItem` on the table ARN and `Query` on the `index/gsi1` ARN only. **Implemented as the CDK task role (Phase 9.2) without any `states:*`**; the design policy in `spec.py` still lists `states:StartExecution` and `DescribeExecution` for activation | Prefix `*` in S3 ARN and condition; `:*` on execution names. No action wildcards, no `*` resource. | `states:StartExecution` and `DescribeExecution` are unused while execution is disabled: attach them only at activation. `DeleteItem` is needed (lock release). | the role and policy, trusted by `ecs-tasks.amazonaws.com` |
| 2 | **State machine role** (CDK, Phase 9.5) | DynamoDB `UpdateItem`, `DeleteItem` on the table; `lambda:InvokeFunction` on the four stage functions | none | none | the role; logging is not enabled, so no log-delivery permission (it would need `logs:*Delivery*` on `*`) |
| 3 | **Pipeline stage role** (design) | `s3:GetObject` on `bronze/*`, `silver/*`; `s3:PutObject` on `bronze/*`, `silver/*`, `gold/*`; `s3:GetObject` on the Gold prefix; `glue:GetTable`, `glue:CreatePartition` on the catalog, one database and one table | `bronze/*`, `silver/*`, `gold/*` prefixes | The `ReadGoldForRegistration` statement is unnecessary: `register_gold_partition` calls Glue only. Remove it. | the role; Glue permissions are the ones the developer identity was denied |
| 4 | **Site bucket policy** (CDK-generated) | Deny all S3 actions over non-TLS; allow `s3:GetObject` to `cloudfront.amazonaws.com` only when `aws:SourceArn` is the energy distribution | `Principal: "*"` appears only in the TLS **Deny**, which is standard and safe | none | created with the stack |
| 5 | **ECS task execution role** | not defined | n/a | n/a | pull from the two ECR repositories; write to the API log group only |
| 6 | **Lambda execution roles for stages** (four, CDK Phase 9.4) | S3 `PutObject`/`GetObject` on the exact layer prefixes only (Bronze put; Silver get Bronze, put Silver; Gold get Silver, put Gold); Glue `GetTable` and `CreatePartition` on the catalog, one database and one table; `logs:CreateLogStream` and `PutLogEvents` on own log group | none | none | none (log group is created by CDK); no ECR statement is needed for the CDK image asset; Glue permission availability unverified |
| 7 | **Scheduler role for reconciliation** | not defined | n/a | n/a | invoke the reconciler (a scheduled job is described in the ADR, not built) |
| 8 | **Frontend deployer** | not defined | n/a | n/a | `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` on the site bucket and `cloudfront:CreateInvalidation` on one distribution |
| 9 | **CDK deployment** | not defined | n/a | n/a | the bootstrap roles (see handover; check whether the game already uses a shared `CDKToolkit` before touching it) |

**Developer identity (`irish-energy-data-platform-dev`)**

- Confirmed in this project: S3 list and read on the Gold prefix (the analytics reader
  succeeded once earlier in this work).
- Reported by the owner: S3 writes for Bronze, Silver and Gold (the first live run).
- Reported denied: Glue (`GetTable`, `CreatePartition`) and Athena query execution.
- Not verified: everything else, including DynamoDB, Step Functions, ECS, ECR, ELB,
  EC2, CloudFront, Cognito, ACM, Route 53, CloudWatch, IAM and CDK bootstrap.

## 6. Deployment blockers

1. **Source is not committed.** Almost all of this work is untracked in Git
   (`frontend/`, `docs/`, most of `src/`, `tests/`, `infrastructure/run_control/`).
   It must be reviewed and committed by the owner before handover.
2. ~~No API container image~~ **Resolved in Phase 9.1:** Dockerfile, `.dockerignore` and
   corrected pinned `requirements-api.txt`; built and run locally. The image is not
   pushed anywhere.
3. ~~No ECS service, target group, ECR or task roles~~ **Resolved as CDK in Phase 9.1**
   (not deployed). The ALB now has an egress rule to the task port. DynamoDB permissions are now on the task role (Phase 9.2). Remaining: the pipeline repository and a first real deployment.
4. ~~DynamoDB table not a CDK construct~~ **Resolved in Phase 9.2** (not deployed). **Step Functions is now a CDK construct (Phase 9.5, not deployed)**; the stage compute exists as Lambda functions (Phase 9.4).
5. **Pipeline compute not deployed** (Lambda container functions are defined in CDK; the image
   must be published and the Glue permissions confirmed; the state machine is defined in Phase 9.5 but not deployed).
6. **No Cognito pool or app client** and no groups.
7. **No logging, retention or alarms.**
8. **Administrator permissions are unconfirmed**, and the developer identity must not
   be widened.
9. **Certificates are not issued** and the DNS records do not exist.
10. **Execution activation is intentionally impossible today** (settings refuse the
    flag, the executor is not wired, no state machine exists). Activation needs a
    separate reviewed code change.

## 7. Security risks

| Risk | Severity | Note |
|---|---|---|
| Plaintext `aws-access-key.txt` present in the working directory (git-ignored, untracked; contents not read) | High | Delete it, rotate the key if it was ever shared, use SSO or roles. |
| ~~Production run history on SQLite if configuration is wrong~~ | Resolved (Phase 9.2) | `API_ENV=production` now refuses to start without `RUN_HISTORY_BACKEND=dynamodb` and a valid table name; the ECS environment sets them. |
| Execution activation changes require new code and review | Medium | By design; keep it a separately approved step. |
| Rate limits and analytics cache are per process | Medium | Multiplied across tasks; add a WAF rate rule at the edge. |
| No WAF, no CSP, third-party font import | Medium | Self-host fonts, add CSP when Cognito domain is known. |
| Tokens held in memory only; PKCE data in browser storage during redirect | Low | Accepted; reload re-authenticates. |
| Access tokens valid until expiry after a group change | Low | Use short lifetimes. |
| Client IPs wrong behind the ALB unless uvicorn uses `--proxy-headers --forwarded-allow-ips` | Medium | Misses auth-failure limits. |
| Public subnets for tasks (public IP, no NAT) | Medium | Task security group restricted to the ALB; S3 and DynamoDB gateway endpoints defined (not yet deployed); remaining internet egress is HTTPS for Cognito keys and ECR. |

## 8. Operational risks

- The DynamoDB store and workflow were never run against real services; the test
  double evaluates only the expressions used. Expect first-deployment fixes.
- Run listing returns at most 200 items and recovery inspects the 50 newest.
- The reconciler is not scheduled; a crashed run is freed only by the 45-minute lease.
- A single API task has no redundancy; the ALB spans two zones but tasks would not.
- The ALB, its two public IPs and the API task cost money continuously (section 10
  of the handover). Deletion protection must be switched off before teardown.
- The game shares the account and the hosted zone: one wrong record edit or a
  re-bootstrap of a shared toolkit stack could affect it.
- Glue registration depends on permissions the developer identity lacks; the stage
  role must carry them.

## 9. Recommended next steps

1. Commit the work after owner review; delete the plaintext key file.
2. Decide the hosting and packaging questions: Fargate API behind the existing ALB
   (recommended by the plan) and Lambda versus Fargate for pipeline stages.
3. Fix `requirements-api.txt`, add a Dockerfile and a non-root image, build and scan
   it locally.
4. Write the remaining CDK constructs: state machine with logging, stage compute, alarms and a budget alert, using the spec policies (ECR, ECS service, DynamoDB and log groups are done).
5. Have an administrator confirm the permissions in handover section 11 and
   create Cognito, certificates and DNS following the handover sequence.
6. Deploy read-only first (execution disabled), run the smoke tests, then request a
   separate approval for activation.

## 10. Ready for administrator deployment?

**Not yet.** The security design, application code and tests are in good shape and the
handover package (`docs/administrator-handover.md`) gives an administrator everything
needed to build the remaining pieces, but blockers 1-9 above must be resolved first.
Activating pipeline execution is explicitly a later, separately approved step.

## Phase 9.4 update

Four pipeline stage Lambda container functions were added (`IrishEnergyPipelineStack`), with one
role each. Verified only through unit tests, synthesised CloudFormation tests and a local Docker
build with invalid-input checks; **never deployed, published to ECR, invoked or tested against
AWS**. The ECS API task role, the DynamoDB and S3 gateway endpoints and the 503 on
`POST /api/runs` are unchanged and execution remains disabled. Remaining before execution:
image publication (CDK bootstrap), Glue permission confirmation, first
supervised run, and re-sizing memory and timeouts from measurements. See
`docs/administrator-handover.md`.

## Phase 9.5 update

`IrishEnergyWorkflowStack` defines the Standard state machine, its own role (DynamoDB
`UpdateItem`/`DeleteItem` on the table, `lambda:InvokeFunction` on the four stage functions) and
the dormant API task role statements `states:StartExecution` (one state machine) and
`states:DescribeExecution` (its executions). Verified only by unit tests, synthesised templates and a
local simulation of the definition against the real handlers and a DynamoDB test double; **never
deployed or executed**. One defect was found and fixed by the simulation (a dropped
`"ResultPath": null` would have let DynamoDB responses overwrite the workflow state). Execution
remains disabled: the settings guard, the 503 on `POST /api/runs` and the unwired executor are
unchanged. Remaining: deployment and bootstrap, image publication, Glue and Step Functions
permission confirmation, a supervised test run, logging and alarms, and the separate activation change.

## Phase 9.6 update

The whole pipeline was exercised locally (about 190 new tests): real handlers and processing code against file-backed
S3, Glue and DynamoDB doubles, the synthesised state machine run by a local interpreter, the real run store and
executor against a Step Functions client double. **No AWS call, deployment or execution took place; real-AWS
behaviour remains unverified.** Changes: Bronze and Silver objects now have run-specific names (retries rewrite
them; concurrent runs cannot collide), transient EirGrid failures are retried 3 times with exponential backoff in
the ingestion state only, and `safe_error_message` no longer crashes on `requests` errors. Open items found and
documented, not changed: Gold keeps one canonical object per run date and the latest successful run for a date replaces it (agreed policy, see the handover document); a failed lock release after success blocks new runs for up to 45 minutes; a `States.Runtime`
failure leaves the run active until the unscheduled reconciler runs; partial failures can lack a `gold_key`; no
cleanup of orphaned Bronze and Silver objects. Security checks over the real synthesised templates found no
change: Lambda roles separate, the state machine role limited to the four stages and the table, the API role
limited to one state machine, no Scan, Batch or table-management permissions, no wildcard resources except the ECR
token, execution disabled.

## Phase 9.6.1 update

Added local, test-double-verified reconciliation and lock recovery: one shared `reconcile` (execution status decides;
running executions keep their lock and have the lease renewed; finished runs are settled and their locks released by the
owning run only; unknown or unqueryable executions change nothing), an executor check that stops a new run starting
behind an expired lease with a live execution, and an optional scheduled reconciler function with its own minimal role
(`states:DescribeExecution` on one state machine, item reads, updates and deletes on the run-history table and `Query` on
its index; no start, put, S3, Glue or other permission). The schedule is not created: the stack is synthesised only with
`-c reconcilerEnabled=true` and the schedule is DISABLED unless separately enabled. The retry error name
`TransientEirGridError` was checked against the Python runtime client and AWS documentation and left unchanged; real AWS
behaviour (including a possible `Lambda.Unknown`) is unverified. Structured logs and EMF metrics are defined; alarms are
proposed only. Execution remains disabled, the lease (45 min) and workflow timeout (30 min) are unchanged and the Gold policy
is unchanged. Nothing was deployed or run against AWS.

## Phase 9.7 update: deployment readiness assessment

Full report: `docs/phase-9.7-production-readiness.md`. **Verdict: READY AFTER SPECIFIED BLOCKERS ARE RESOLVED.** The code,
synthesised templates and local tests are in good shape (1,070 backend and infrastructure tests, 73 frontend tests, build and
lint, five synthesis configurations, no dependency cycle, no `Action: *`, one unavoidable `Resource: *` for
`ecr:GetAuthorizationToken`), but material AWS behaviour is unverified. Blockers: no verified deployment identity or
CDK bootstrap state (a shared `CDKToolkit` with the game is possible); the supplied API certificate is unverified and the
`us-east-1` frontend certificate does not exist; the Cognito pool and client are external and unverified. Should be
resolved before deployment: Fargate-to-Cognito/ECR/CloudWatch connectivity with public IPs and no NAT (JWKS is fetched
lazily, so the health check cannot detect a broken path); the account-conditioned trust on the Lambda roles; image
publication and bootstrap repository policy; Glue and Lake Formation; frontend publishing; the unpinned data lake stack;
the plaintext key file in the project directory (git-ignored, contents not read: remove and rotate). Estimated cost: about
EUR 38 to 40 a month minimum (load balancer, three public IPv4 addresses, one Fargate task), about EUR 50 to 60 conservative,
about EUR 33 to 40 more if NAT were added; prices are unverified estimates. Execution remains disabled and the reconciler
schedule remains disabled by default.

## Phase 9.7.1 update

The data lake stack is now account-pinned and a wrong active account stops synthesis before any stack exists; production
frontend builds fail without the required settings; the stack-count statement in the DNS and certificates guide is corrected.
Details and remaining risks: `docs/phase-9.7.1-local-corrections.md`.
