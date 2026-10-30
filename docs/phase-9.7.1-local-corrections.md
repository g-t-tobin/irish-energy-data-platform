# Phase 9.7.1: local production-readiness corrections

Status: local changes only. Nothing was deployed or bootstrapped, no AWS API was called, no credential file was read,
and no permission, DNS record, certificate or game resource was touched. Pipeline execution is still disabled
(`PIPELINE_EXECUTION_ENABLED=true` still stops start-up) and the reconciler schedule is disabled by default. Nothing is
staged or committed. Baseline: Phase 9.7 (uncommitted) on top of `41140a8`.

## 1. Account pinning (finding H-7)

- `infrastructure/energy_config.py`: new `validate_deployment_account(env)`. It reads only `CDK_DEFAULT_ACCOUNT` from the
  environment and raises `ConfigError` unless it is exactly `851725545966`. **Correction:** a missing, empty or
  whitespace-only value is also refused (the first version allowed it); offline synthesis must set it explicitly:
  `CDK_DEFAULT_ACCOUNT=851725545966 CDK_DEFAULT_REGION=eu-west-1 cdk synth`. `load_edge_config` keeps its own wrong-account check.
- `infrastructure/app.py`: the check runs before any stack is created, and `IrishEnergyDataLakeStack` now uses
  `cdk.Environment(account=ACCOUNT, region=REGION)` like every other stack. `CDK_DEFAULT_REGION` no longer influences any
  stack. The data lake template is unchanged apart from resolved account values.
- Still open: whether the Glue database, table and Athena workgroup already exist (conflict on first deploy).

Negative test: with `CDK_DEFAULT_ACCOUNT=111111111111` (or missing, empty or blank) synthesis exits with
`Configuration error: Refusing to synthesize ...` naming `851725545966` and writes no templates, for
the default, certificate, service and reconciler configurations.

## 2. Documentation corrections (finding L-1)

`docs/deployment-dns-and-certificates.md` section 4 said synthesis without certificates produces only the data lake stack. Now:
5 stacks without certificates, 7 with both certificates (the service does not add a stack), 8 with the optional reconciler.
Other docs were checked: `docs/phase-9.7-production-readiness.md` (marked resolved), `README.md`, the handover and the
readiness review (notes added); no other stale count was found.

## 3. Credential file (finding H-1), names and metadata only

- `aws-access-key.txt` exists in the project root, is 122 bytes with mode 644 (readable by every local user), is git-ignored,
  untracked and has never appeared in any commit on any ref. Its contents were not read.
- `irish-energy-data-platform-public-key-git` (mode 600) and its `.pub` are also ignored and untracked; the first name suggests
  a private key, so it deserves the same review.
- The only tracked env-like file is `frontend/.env.example`, with placeholders.
- Recommended, for the owner (not done here): rotate the access key in IAM, delete the file, use a profile or SSO, and
  `chmod 600` anything that must stay. Tests assert the names are ignored, untracked and absent from history.

## 4. Lambda trust condition (finding H-3): inconclusive, no change

Every Lambda execution role trusts `lambda.amazonaws.com` with `StringEquals` on `aws:SourceAccount`. The IAM global
condition key documentation says the key is present only when a call is made directly by an AWS service principal on behalf
of a resource, and that not all service integrations support it. The Lambda execution-role documentation shows only the plain
service-principal trust. So the key might be absent when Lambda assumes the role, and because `StringEquals` fails when the
key is missing, the functions could then fail at first invocation. This cannot be settled from documentation or locally.

Decision: leave the condition unchanged. After deployment, in a supervised session, invoke one function with a harmless
invalid payload; a role-assumption error means the condition must change. The fallback (proposed, not applied) is
`StringEqualsIfExists`, which keeps the protection when the key is present and does not block when it is absent. This is
the only fix to apply if the check fails; it needs no new AWS permission.

## 5. Frontend production configuration (finding H-6, partly)

Problem: a production build without `VITE_API_BASE_URL` silently baked in `http://127.0.0.1:8001`, and an `http` localhost
Cognito redirect was accepted.

- `frontend/src/config/productionEnv.ts`: `validateProductionEnv` requires `VITE_API_BASE_URL` to be exactly
  `https://energy-api.ai-autofy-general-server.com` (no path, query, fragment or credentials), the Cognito variables to be
  valid, and both redirects to equal `https://energy.ai-autofy-general-server.com/`. Messages name variables, never values.
- `frontend/vite.config.ts`: runs it for `vite build` in production mode and fails the build with the list of problems.
- `frontend/src/api/baseUrl.ts`: also throws at start-up in a production bundle without the API URL.
- `npm run build:local` builds in development mode without the check; its output must not be deployed.
- `frontend/.env.example`: comment-only production notes.
- The app has no WebSocket use, so no `wss` value is needed. Game hostnames are rejected by the exact-origin check.

Verified locally: a build with nothing set fails and lists six variables; one with a localhost API URL fails; one with the
production hostnames and placeholder Cognito values succeeds and the bundle contains the energy API host. Cognito values are
placeholders; the real pool and client remain unverified (C-3).

## 6. Security regression review

No new AWS permission, resource, route or credential access was added. Checked by tests and review: only
`src/executor_stepfunctions.py` calls `start_execution`; no API route invokes a Lambda or stage; execution stays disabled;
the reconciler schedule stays `DISABLED` unless `reconcilerScheduleState=ENABLED` is passed; game hostnames appear only in the
reserved list in `infrastructure/energy_config.py`; credential-like files are not tracked.

## 7. Results (all local)

| Check | Result |
|---|---|
| Backend and infrastructure tests | 1088 passed, 2 skipped (1070 before, plus 18 new in `tests/test_local_corrections.py`) |
| Ruff | 205 findings (unchanged from the Phase 9.6.1 baseline) |
| Frontend tests, lint, build | 90 passed (73 before, plus 17); lint clean; production build passes with valid values and fails without |
| Synthesis, no certificates | 5 stacks |
| Synthesis, certificates | 7 stacks |
| Synthesis, certificates and ECS service | 7 stacks |
| Synthesis, plus reconciler | 8 stacks, schedule `DISABLED`; `ENABLED` only with the explicit context |
| Wrong account (`111111111111`) | refused, no templates, exit code 1 |
| All stack environments | `aws://851725545966/eu-west-1` |

## 8. Remaining risks

C-1 (deployment identity and bootstrap), C-2 (certificates), C-3 (Cognito), H-1 (plaintext key file, owner action),
H-2 to H-5, H-8, and the Medium and Low items of the Phase 9.7 report are unchanged. H-3 now has a defined supervised check.
