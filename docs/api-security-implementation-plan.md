# API security-hardening implementation plan

Status: planning and test design only. No application code, tests, dependencies,
infrastructure or environment files were changed to produce this plan, and
nothing in it has been implemented. Companion to
[security-and-production-architecture.md](security-and-production-architecture.md),
which holds the risk ranking and target architecture.

Conventions used throughout: **[Verified]** was observed in the repository,
**[Assumption]** is believed but unconfirmed, **[Decision]** needs your
confirmation before implementation.

## 0. Baseline facts and assumptions

**[Verified] from reading the repository**
- `src/api.py` defines five routes (listed in section 1) with no authentication, plus FastAPI's default `/docs`, `/redoc` and `/openapi.json`.
- CORS: two hard-coded localhost origins, methods `GET`/`POST`, header `Content-Type`, credentials off.
- `POST /api/runs` creates a run record and starts `execute_pipeline` in a daemon thread. That function calls the real ingestion, transformation, Gold and Glue code using the process's AWS identity.
- `GET /api/analytics/daily-demand` returns `source` as `s3://<bucket>/gold/eirgrid/daily_demand/`. Its error text includes an S3 object key when a Gold file is missing columns (`src/analytics.py`).
- Run records returned by `/api/runs*` include `bronze_key`, `silver_key`, `gold_key` and an `error` string (redacted, truncated to 300 characters, but still free text).
- `RunStore` (`src/run_store.py`) is SQLite-only; there is no storage interface or alternative implementation.
- `requirements-api.txt` contains only `fastapi` and `uvicorn[standard]`; `httpx` is **not** installed, so FastAPI's `TestClient` cannot be used today. Existing API tests call handler functions directly, which bypasses dependencies and middleware.
- Tests live flat in `tests/`; `tests/unit`, `tests/integration`, `tests/contract` and `tests/data_quality` exist but are empty. There is no `tests/conftest.py`.
- The frontend (`frontend/src/App.tsx`) makes two plain `fetch` GETs, sends no credentials, has no auth code and no test framework (no Vitest/Jest in `package.json`). The Run Pipeline button makes no API call.
- There is no Dockerfile, no `.env.example`, and `infrastructure/` contains only the data-lake CDK stack.
- Last recorded results (earlier sessions, **not re-run for this task**): 112 backend tests passed; frontend build and lint passed.

**[Assumption]**
- Amazon Cognito can be created in the target account by an administrator. Not verified, and the developer identity is not assumed to have permission.
- One environment, region `eu-west-1`, a small, administrator-managed user base.
- Hosting (ECS Fargate behind an ALB, or API Gateway + Lambda) is undecided; the plan is written to work with either.

## 1. Endpoint inventory

| Endpoint | Today | What it does | Target access | No/invalid token | Insufficient role |
|---|---|---|---|---|---|
| `GET /health` | Public | Returns `{"status":"ok"}`; no data | Public (load-balancer check); must stay minimal | n/a | n/a |
| `GET /api/runs` | Public | Reads run history; exposes S3 keys, error text | `viewer` | 401 | 403 |
| `GET /api/runs/{run_id}` | Public | Reads one run; same fields | `viewer` | 401 | 403 |
| `GET /api/analytics/daily-demand` | Public | Reads Gold Parquet from S3 (cost per cache miss); exposes bucket name | `viewer`, rate limited | 401 | 403 |
| `POST /api/runs` | Public | Starts a real pipeline: EirGrid fetch, S3 writes, Glue attempt | `operator` AND execution enabled | 401 | 403; operator with execution disabled gets the disabled response (section 3) |
| `/docs`, `/redoc`, `/openapi.json` | Public | API schema | Disabled outside local development | 404 | 404 |

Response conventions (keep FastAPI's `{"detail": "..."}` body for compatibility):
- **401** for a missing, malformed, expired, wrongly signed or wrong-issuer/client token. The same generic detail (`"Not authenticated"`) is returned for all of them so clients cannot probe why; the precise reason goes to the server log. Header: `WWW-Authenticate: Bearer`, plus `error="invalid_token"` when a token was supplied.
- **403** for a valid token whose groups do not include the required role.
- **404** for a missing run (only after authorization succeeds, so run IDs cannot be enumerated anonymously).
- **429** with `Retry-After` when rate limited.
- **503** with a fixed message for storage or S3 unavailability (no AWS detail).
- **500** for unhandled errors: `{"detail": "Internal server error", "request_id": "..."}`; no traceback.

## 2. Authentication and authorization design

### 2.1 Roles
- `viewer`: Cognito group `viewer`; may call the read endpoints.
- `operator`: Cognito group `operator`; everything a viewer can do plus `POST /api/runs`. The server treats `operator` as a superset of `viewer`; users do not need both groups.
- No group, or only unknown groups: 403 on every protected route.

### 2.2 Token verification in FastAPI
Library **[Decision]**: `PyJWT[crypto]` (small, maintained, supports JWKS fetch with caching). Alternatives (`python-jose`, `authlib`) add more surface.

Verification order, performed by a single FastAPI dependency so no route can skip it:
1. Require `Authorization: Bearer <token>`; reject other schemes and multiple headers.
2. Read the unverified header only to get `kid` and `alg`. Allow-list `alg == "RS256"`; reject `none`, HS* and anything else. Never use `jku`, `x5u` or other headers to choose a key location.
3. Fetch signing keys only from the URL derived from configuration: `https://cognito-idp.<region>.amazonaws.com/<pool_id>/.well-known/jwks.json`. Cache keys; on an unknown `kid` refetch at most once per cooldown window (rotation without letting garbage tokens trigger unlimited fetches). Short network timeout; failure to fetch means reject (fail closed), logged.
4. Verify the signature, then claims: `iss` equals the exact configured issuer; `exp` required and not passed; `iat`/`nbf` checked with a small leeway (about 30 seconds); `token_use == "access"` (reject ID tokens); `client_id` equals the configured app client ID. Cognito **access tokens carry `client_id`, not `aud`**, so audience is checked via `client_id`. **[Decision]**: accept access tokens only.
5. Roles come only from the verified `cognito:groups` claim. Any role information in headers, query strings, bodies or the frontend is ignored.
6. Produce a small `Principal(sub, groups, role)` object; authorization dependencies `require_viewer` and `require_operator` compare against it.

Known limits to accept and document: access tokens stay valid until they expire (set a short access-token lifetime in Cognito, for example 30-60 minutes) and removing a user from a group takes effect when the next token is issued.

### 2.3 Configuration and safe defaults (new `src/settings.py`)
All read from environment variables, validated at startup; any invalid or missing required security setting aborts startup in non-local environments.

| Variable | Default | Purpose |
|---|---|---|
| `API_ENV` | `production` (fail-safe) | `local` or `production`; only `local` may relax anything |
| `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID` | none | Required unless a documented local test mode is used |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated exact origins; production requires `https://`, rejects `*` and localhost |
| `PIPELINE_EXECUTION_ENABLED` | `false` | Master switch for `POST /api/runs` |
| `ENABLE_API_DOCS` | `false` | Exposes `/docs` etc. |
| `RATE_LIMIT_*` | conservative | See section 5 |

Local development **[Decision]** (choose one):
- **Option A (recommended)**: no authentication bypass exists in the code. Tests use a locally generated RSA key pair and a stub JWKS. For manual UI development, use a real dev Cognito pool once one exists.
- **Option B**: a guarded dev bypass (`API_ENV=local` and an explicit flag, loopback bind only, refuses to start otherwise, grants `viewer` only, never `operator`). Convenient but is a bypass in the code path; it needs its own tests and review.

### 2.4 Frontend authentication
- Cognito app client for a public SPA: authorization code grant with PKCE, no client secret, exact callback and sign-out URLs, Cognito hosted UI for credentials (the app never handles passwords).
- Use `oidc-client-ts` rather than writing PKCE/state handling by hand **[Decision]** (new frontend dependency).
- Keep tokens in memory only (no `localStorage`/`sessionStorage`) to limit exposure to XSS. Consequence: a page reload requires a redirect to the hosted UI, which usually completes silently while the Cognito session is active.
- Wrap `fetch` so every API call adds `Authorization: Bearer <access_token>`; on 401 start sign-in again; on 403 show an "insufficient permissions" message. The UI may use group claims to show or hide controls, but this is cosmetic; the server enforces everything.
- **CSRF**: the API uses bearer tokens in a header, not cookies, so browsers do not attach credentials automatically and classic CSRF does not apply. Keep `allow_credentials=False`. The OAuth `state` parameter and PKCE (handled by the library, and the callback must validate them) protect the login flow itself. If cookies or a backend-for-frontend are ever introduced, SameSite cookies and CSRF tokens become mandatory.
- **XSS** is the real risk to in-memory tokens: keep React's default escaping (no `dangerouslySetInnerHTML`, currently true), self-host fonts, add a Content-Security-Policy at CloudFront, pin and review dependencies.
- CORS (tightened at the same time): exact CloudFront origin(s) from configuration, methods `GET`/`POST`/`OPTIONS`, headers `Authorization` and `Content-Type`, `max_age` set. CORS is not authentication.

## 3. Safe pipeline execution

Goals and how each is met:

| Requirement | Mechanism |
|---|---|
| Disabled by default | `PIPELINE_EXECUTION_ENABLED=false` default; a `PipelineExecutor` interface whose default implementation is `DisabledExecutor` |
| Unauthenticated cannot start | JWT dependency runs first: 401 |
| Viewers cannot start | `require_operator`: 403 |
| Operators blocked while disabled | After authorization, the route checks the executor; `DisabledExecutor` returns the disabled response and creates no run record and no thread. **[Decision]** status code: 503 with a fixed detail (recommended) versus 403/409 |
| No accidental execution in development or tests | The in-process thread executor is removed from the default path. It exists only behind `API_ENV=local` plus an explicit flag, and is not wired in tests. A `tests/conftest.py` autouse fixture installs a `ForbiddenExecutor` that raises if called, clears AWS profile/credential environment variables, sets `AWS_EC2_METADATA_DISABLED=true`, and makes any unmocked `boto3.client` creation fail |
| Production uses Step Functions with a least-privilege role | `StepFunctionsExecutor` (phase 6) calls only `StartExecution` on one state machine ARN; the pipeline steps run under a separate role (see the production document, Section 6) |
| Duplicate requests | Optional `Idempotency-Key` header stored with the run (and with a time-to-live in the shared store); a repeat returns the existing run. Step Functions execution names equal the run ID so a repeated start cannot create a second execution |
| Concurrent executions | The single-active-run check stays atomic: the SQLite transaction today; a DynamoDB conditional write on a lock item with a lease expiry in the shared store |
| Failures | State-machine `Catch` branches record `failed` or `partial_failure` with a coarse error code; the state machine has an overall timeout; the lock is released in a final step and expires by lease if everything crashes |
| Interrupted runs | Local: existing startup recovery to `interrupted`. Shared: a reconciliation step that, for non-terminal runs past their lease, calls `DescribeExecution` (needs `states:DescribeExecution`) and sets the real outcome or `interrupted`. **[Decision]** reconciliation on read versus a scheduled job |

Nothing in this plan calls `POST /api/runs` or runs the pipeline.

## 4. Response sanitization

**Fields returned to `viewer` (and `operator`) for runs**: `run_id`, `status`, `stage`, `days_back`, `created_at`, `updated_at`, `started_at`, `finished_at`, `error_code` (new), and `error` containing a fixed human-readable message chosen from the code. Removed from API responses: `bronze_key`, `silver_key`, `gold_key` and any free-text exception content. Keeping the key named `error` preserves the existing contract that the frontend reads; its value becomes a fixed message such as "Pipeline stage failed (upstream service)". Initial `error_code` set: `UPSTREAM_UNAVAILABLE`, `VALIDATION_FAILED`, `STORAGE_ERROR`, `ACCESS_DENIED`, `INTERRUPTED`, `INTERNAL_ERROR`.

Internal detail: keep the redacted exception text in an internal column (for example `error_detail`, added with `ALTER TABLE ... ADD COLUMN`) and in server logs, never serialized. Implement a single `to_public_run()` function with an explicit field whitelist so new columns are private by default.

**Analytics**
- `source` currently reveals the bucket name. Replace the value with a non-sensitive label (for example `gold/eirgrid/daily_demand`, no bucket) while keeping `source` as a string so the frontend validation and display still work.
- `AnalyticsUnavailable` messages must not include S3 keys, column lists or AWS codes; return a fixed message, log the detail.
- Metric and quality fields are unchanged.

**Logging**: structured JSON logs with a request ID (also returned as `X-Request-ID`), method, route template, status, latency, `sub` and role. Never log the `Authorization` header, tokens, request bodies, or full exception messages from AWS without redaction. Tracebacks stay in server logs only. Add API response headers `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`; HSTS is set at CloudFront/ALB.

## 5. Rate limiting and input validation

- Existing validation stays: `days` 1-90, `days_back` 1-30. Add a maximum request-body size and keep `run_id` parameterized-only (already true); consider a UUID format check on `run_id` to reject junk early.
- Cache: bounded by the 90 possible `days` values; add a test asserting it stays bounded and that unauthenticated requests never reach the reader.
- Rate limits **[Decision]** (starting values): per-user `GET` endpoints 60/minute, analytics 30/minute, `POST /api/runs` 5/hour. Implement a small in-memory token bucket keyed by `sub` (authenticated) and by client IP for failed authentication. This is per process, so with several tasks the effective limit multiplies; add a WAF rate rule at deployment as the shared control. Behind a load balancer, only trust `X-Forwarded-For` from the configured proxy (uvicorn `--proxy-headers` with `--forwarded-allow-ips` set to the proxy range), otherwise clients can spoof their IP.
- Failures to authenticate are counted before expensive work (JWKS refetch cooldown, no S3 access).

## 6. Test plan

New dev dependency **[Decision]**: `httpx` (required by FastAPI `TestClient`), in a new `requirements-dev.txt`, so authorization, headers and middleware can be tested at HTTP level. Without it the plan cannot verify 401/403 behaviour, headers or CORS.

| Area | Test file (new unless noted) | Cases |
|---|---|---|
| Test harness | `tests/conftest.py`, `tests/auth_helpers.py` | RSA key pair and stub JWKS generator; token factory (valid, expired, wrong issuer, wrong client, ID token, `alg=none`, HS256 forged with the public key, missing `kid`, unknown `kid`, tampered payload, wrong groups); autouse guards: forbidden executor, no AWS environment, no real `boto3.client` |
| Settings | `tests/test_settings.py` | Safe defaults; execution and docs default off; production requires Cognito settings and https origins; wildcard/localhost rejected in production |
| JWT verification | `tests/test_auth_jwt.py` | Missing header, wrong scheme, malformed token, empty bearer, expired, not-yet-valid, invalid signature, invalid issuer, wrong client ID, ID token rejected, algorithm allow-list, JWKS fetch failure fails closed, key rotation refetch with cooldown, no token or key in logs |
| Authorization | `tests/test_api_authorization.py` | Each endpoint: no token 401 with `WWW-Authenticate`; viewer allowed on reads, 403 on POST; operator allowed on reads; role headers/body fields ignored; direct API call without frontend restrictions still enforced; `/health` public; docs routes 404 by default |
| Execution gate | `tests/test_pipeline_execution_gate.py` | Default disabled; operator gets the disabled response and no run record or thread is created; viewer and anonymous never reach the gate; enabled with fake executor starts exactly one run; duplicate idempotency key; second concurrent start 409; production config cannot enable the local thread executor; `execute_pipeline` and AWS clients never called by default |
| Sanitization | `tests/test_api_sanitization.py` | Responses contain only whitelisted fields; no S3 keys, bucket names or raw errors; failure writes `error_code`; 500 handler returns request ID without traceback; storage and analytics errors return fixed messages; log output contains detail but no tokens or secrets |
| Contract | `tests/test_api_contract.py` | Existing shapes preserved: `{"runs": [...]}`, run fields the frontend reads, analytics `source`/`days` and quality fields; documented intentional differences (removed key fields, sanitized `error`, `source` label) asserted explicitly |
| Rate limiting | `tests/test_rate_limit.py` | Within limit passes; over limit 429 with `Retry-After`; separate buckets per user and per endpoint; failed-auth limiter; proxy header only trusted when configured |
| Analytics | `tests/test_analytics.py` (modify) | Fixed unavailable message without keys; `source` label; cache bounded; invalid `days` rejected at HTTP level |
| Run store | `tests/test_run_store.py` (modify), `tests/test_api_runs.py` (modify) | New columns and migration of an existing database; `to_public_run` whitelist; existing tests adapted to the executor abstraction and auth fixtures |
| Shared store (phase 6) | `tests/test_dynamodb_run_store.py` | Contract tests shared with the SQLite store; conditional lock and lease expiry; mocked client or `moto` **[Decision]** |
| Step Functions (phase 6) | `tests/test_executor_stepfunctions.py` | `StartExecution` called once with deterministic name; error mapping; mocked client only |
| Frontend | `frontend/src/auth/*.test.ts` (new, requires Vitest **[Decision]**) | Token attached, 401 triggers sign-in, 403 message, tokens never written to storage |
| Existing suites | `.venv/bin/python -m pytest -q`, `npm run build`, `npm run lint` | Must pass after every phase. Nothing is claimed to pass until actually run |

## 7. Implementation sequence

Each phase is independently testable and mergeable. Phases 1-5 need no AWS access.

### Phase 1: Security configuration and safe defaults
- **Files**: new `src/settings.py`, new `src/executor.py` (interface plus `DisabledExecutor`), `src/api.py`, new `tests/conftest.py`, `tests/test_settings.py`, `tests/test_pipeline_execution_gate.py` (disabled-path cases), `tests/test_api_runs.py`, `requirements-dev.txt`.
- **Changes**: load validated settings; configurable CORS origins; docs off by default; execution behind `PIPELINE_EXECUTION_ENABLED`; thread executor removed from the default path; test guards.
- **Acceptance**: with defaults `POST /api/runs` starts nothing; docs routes return 404; invalid production settings stop startup; all earlier tests pass.
- **Prerequisites**: decisions on `httpx`, the disabled status code and local-development option A or B.

### Phase 2: JWT verification and authorization dependencies
- **Files**: new `src/auth.py`, `tests/auth_helpers.py`, `tests/test_auth_jwt.py`, `requirements-api.txt` (add `PyJWT[crypto]`).
- **Changes**: verifier and `Principal`, `require_viewer`, `require_operator`, JWKS cache with cooldown.
- **Acceptance**: every case in the JWT table passes with a stub JWKS; no network access in tests.
- **Prerequisites**: Cognito pool and client identifiers as configuration names only (real values not needed yet); library decision.

### Phase 3: Protected read-only endpoints
- **Files**: `src/api.py`, `src/run_store.py` (public serializer, new columns), `src/analytics.py` (messages, `source`), `tests/test_api_authorization.py`, `tests/test_api_sanitization.py`, `tests/test_api_contract.py`, modified `tests/test_analytics.py` and `tests/test_run_store.py`.
- **Changes**: attach dependencies to each route; whitelist serializer; sanitized errors and analytics label.
- **Acceptance**: 401/403 matrix passes; no bucket, S3 key or raw error appears in any response; contract tests pass with the documented differences.
- **Prerequisite**: phase 2.

### Phase 4: Frontend authentication integration
- **Files**: `frontend/src/auth/*`, `frontend/src/App.tsx`, `frontend/src/App.css` (minimal), `frontend/package.json`, optional Vitest config and tests.
- **Changes**: sign-in with PKCE, in-memory tokens, authenticated fetch wrapper, 401/403 handling, display `error` field and drop use of removed fields. Run Pipeline button stays a local demonstration.
- **Acceptance**: build and lint pass; frontend tests pass; no token in browser storage; signed-out users see a sign-in state, not data.
- **Prerequisites**: phase 3; a Cognito app client to test against (needs an administrator) or a local mock issuer for tests.

### Phase 5: Rate limiting, logging and error sanitization
- **Files**: new `src/ratelimit.py`, new `src/logging_config.py`, `src/api.py`, `tests/test_rate_limit.py`, `tests/test_security_headers_logging.py`.
- **Changes**: limiter, request-ID middleware, JSON logging with redaction, global exception handler, security headers.
- **Acceptance**: limit and header tests pass; logs contain no tokens; tracebacks never in responses.
- **Prerequisite**: phase 1; limit values confirmed.

### Phase 6: Shared run history and Step Functions architecture
- **Files**: `src/run_store.py` (extract a store protocol), new `src/dynamodb_run_store.py`, new `src/executor_stepfunctions.py`, an ADR in `docs/adr/`, tests for both.
- **Changes**: DynamoDB store with conditional lock and lease; Step Functions executor; state-machine definition designed (not deployed) with `Catch`, timeout and status updates; reconciliation approach chosen; decision on how the existing pipeline code is packaged to run as steps.
- **Acceptance**: store contract tests pass for SQLite and DynamoDB (mocked); executor tests pass; no AWS calls in the suite.
- **Prerequisites**: hosting decision; administrator confirmation of DynamoDB and Step Functions permissions; packaging decision (pandas/pyarrow size affects Lambda versus a Fargate task).

### Phase 7: Read-only AWS deployment
- **Files**: new CDK stack(s) in `infrastructure/` for Cognito, frontend hosting and the read-only API; Dockerfile (if Fargate); CI/CD notes.
- **Changes**: deploy with `PIPELINE_EXECUTION_ENABLED=false`, runtime role limited to Gold-prefix S3 read (and DynamoDB when ready), CORS set to the real origin, HTTPS, alarms and a budget alert.
- **Acceptance**: `cdk diff` reviewed by a human before deploy; smoke tests: anonymous request 401, viewer reads, operator POST returns the disabled response; no new permission on the developer profile.
- **Prerequisites**: phases 1-5 complete; an administrator creates or approves the infrastructure and IAM roles; domain decision if using an ALB.

### Phase 8: Separately reviewed activation of pipeline execution
- **Files**: configuration only plus the Step Functions stack and `PIPELINE_EXECUTION_ENABLED` change, with its own review record.
- **Changes**: enable the Step Functions executor for `operator`; frontend control wired to the real endpoint only in this phase.
- **Acceptance**: a documented review of the execution role, alarms and rollback; the first real run is a supervised single run by an operator; kill switch (set the flag false) verified.
- **Prerequisites**: phases 6 and 7 complete; your explicit approval.

## 8. Decisions still requiring confirmation

1. JWT library: `PyJWT[crypto]` (recommended).
2. Local development: Option A (no bypass) or Option B (guarded bypass).
3. Status code when execution is disabled: 503 (recommended), 403 or 409.
4. Accept access tokens only (recommended) and the token lifetime to request from the administrator.
5. Frontend auth library (`oidc-client-ts` recommended) and whether to add Vitest.
6. Add `httpx` and a `requirements-dev.txt` for HTTP-level API tests.
7. Whether operators may see S3 keys (plan: no, logs only).
8. Rate limit values and in-memory limiter versus relying on WAF.
9. Hosting: ECS Fargate + ALB (needs a domain and certificate) or API Gateway + Lambda.
10. Interrupted-run reconciliation: on read or scheduled.
11. `moto` versus hand-mocked clients for DynamoDB tests.
12. How the pipeline code is packaged for Step Functions.

## 9. Prerequisites that are not yet verified

- Permission for an administrator to create Cognito, DynamoDB, Step Functions, ECS/ALB or API Gateway, CloudFront and IAM roles. Nothing here has been checked against AWS and the developer profile must not be broadened.
- The full backend and frontend suites have not been re-run for this document, since no code changed.
