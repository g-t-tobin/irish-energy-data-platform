# Phase 9.7.2.1: permissions boundary and deployment policy hardening

Status: local policy and documentation work only. No AWS API was called, no IAM resource was created or changed, nothing was
bootstrapped or deployed, no image was pushed, no credential was read, and nothing is staged or committed. The CDK application was **unchanged** in this phase: `src/`, `infrastructure/` and `frontend/` had no differences when Phase 9.7.2.1 ended. Phase 9.7.2.2 later changed one line in `infrastructure/stacks/energy_edge_stacks.py`: the application load balancer now has the fixed name `irish-energy-api` (`load_balancer_name`), so that listener permissions can name it. Pipeline execution stays
disabled and the reconciler schedule stays disabled by default. Baseline: commit `4fdd2fe`.

## 1. Summary of changes

| Change | File |
|---|---|
| Proposed permissions boundary for all nine runtime roles (initial) | `docs/iam/runtime-permissions-boundary.json` |
| Boundary variant for a future, separately approved execution activation | `docs/iam/runtime-permissions-boundary-execution-activation.json` |
| New role-creation policy for the CloudFormation execution role | `docs/iam/iam-role-management-policy.json` |
| Optional frontend publishing policy | `docs/iam/frontend-publishing-policy.json` |
| IAM role actions removed from the execution policy | `docs/iam/cloudformation-execution-policy.json` |
| Boundary protection added to the deployer | `docs/iam/deployment-identity-policy.json` |
| Tests | `tests/test_iam_boundary.py` (new), `tests/test_iam_policy_package.py` (extended) |
| Documents | `docs/iam/README.md`, `docs/phase-9.7.2-administrator-preparation.md`, `docs/administrator-handover.md`, `docs/production-readiness-review.md`, `README.md`, this report |

`passrole-policy.json`, `lambda-deployment-policy.json` and `ecs-deployment-policy.json` were reviewed and need no change: the
PassRole patterns are now also the set of roles that may be created, and neither the Lambda nor the ECS policy touches IAM.

## 2. Boundary design

The boundary `irish-energy-runtime-boundary` (a **proposed name**, not an existing resource) is the permissions boundary of
all nine runtime roles: four pipeline Lambda roles, the reconciler Lambda role, the Scheduler invocation role, the Step
Functions role, and the ECS task execution and application task roles. It is derived from the actual statements of those
roles in the synthesized templates. Each role's effective permissions are the intersection of its identity policy and the
boundary, so the boundary is a ceiling that equals what each role needs today.

| Requirement | How the boundary meets it |
|---|---|
| Only the services the platform uses | actions from `logs`, `s3`, `glue`, `dynamodb`, `states`, `lambda` and `ecr` only. No `iam`, `sts`, `ec2`, `kms`, `ssm`, `secretsmanager`, `cloudformation` or `cognito-idp` (test) |
| Preserve the least-privilege design | the sensitive statements are tied to one role each with `ArnLike` on `aws:PrincipalArn` (for example only the Silver role may read the bronze prefix, only the reconciler role may touch the run table without `PutItem`); a test evaluates every statement of every synthesized role against the boundary with a local model of policy matching (not the AWS policy engine) and finds no gap |
| No wildcard administrative permissions | allow-list only, no wildcard action, `Resource: *` only for `ecr:GetAuthorizationToken` and two explicit Deny statements |
| No game resources | every resource is a project ARN (bucket `irish-energy-data-platform-851725545966`, `irish-energy-*` functions and log groups, the run table, the state machine, `irish-energy-api`); a test rejects game or foreign names and other accounts |
| Not removable or modifiable by the deployment identity | section 5: explicit Deny of policy changes and boundary removal for both the deployer and the execution role, and no Allow for any of those actions anywhere in the package |
| No broader permissions through inline or attached policies | the boundary caps every identity policy on the role; the execution role cannot attach managed policies at all and cannot write an inline policy to a role without the boundary |
| Works with ECS, Lambda, Step Functions and Scheduler | the boundary only restricts actions; trust policies are untouched, each service still assumes its role, and the evaluation test covers all four service types |

Statement groups: `Logs` (project log groups), `BronzeWrite`, `SilverRead`, `SilverWrite`, `GoldRead`, `GoldWrite`,
`GluePartitions`, `ApiList`, `ApiRead`, `ApiItems`, `ReconWorkflowItems`, `Query`, `DescribeRuns`, `WorkflowInvoke`,
`SchedulerInvoke`, `EcsPull`, `EcsToken`, and `DenyStartExecution`. Size: 5,516 of 6,144 permitted characters (activation
variant 5,935).

## 3. Limitations of permissions boundaries

Do not treat the boundary as more than it is.

1. **It grants nothing and does not restrict the CloudFormation execution role.** It applies only to roles that carry it. The
   execution role is limited by its own identity policies (section 5). Do not attach this boundary to the execution role or to
   any bootstrap role: it would break them. The bootstrap option `--custom-permissions-boundary` applies to bootstrap roles and
   is not this boundary.
2. **It only works if roles are created with it.** CloudFormation sets it when the application is told to (section 8). Until
   then the execution role's Deny and Allow conditions make every role creation fail, which is the safe failure.
3. **It cannot restrict the trust policy.** IAM has no condition key for the principal inside a role's trust policy at
   `iam:CreateRole` or `iam:UpdateAssumeRolePolicy`. A template could name a different principal. The boundary still caps what
   that role could do, and the nine role-name patterns, `cdk diff` review and the trust-policy mapping test limit the risk.
4. **Resource-based policies are outside it (bypass).** Per AWS policy-evaluation rules, a resource-based policy that names a role ARN
   directly can grant access that the boundary does not limit. The data lake bucket policy, the ECR repository policies and
   any state machine or Lambda resource policy should be reviewed for grants to project roles (confirm against the current IAM
   documentation).
5. **Per-role ceilings rely on role names.** `aws:PrincipalArn` is matched against the name patterns. This is safe because the
   execution role can only create roles with those names, but a renamed role would lose its ceiling and fail closed (AccessDenied).
6. **Some ceilings are slightly wider than one role needs**, to stay within the 6,144-character limit: `Logs` and `EcsToken`
   apply to every runtime role (resource-scoped to project log groups; `ecr:GetAuthorizationToken` only returns a token), and
   `ReconWorkflowItems` also lets the Step Functions role call `GetItem`, which its identity policy does not grant. These are
   ceilings, not grants.
7. **Service-linked roles, SCPs and session policies are separate mechanisms.** The boundary does not affect service-linked roles.
8. **It must change with the roles.** When a role legitimately needs a new action, both the CDK role and the boundary change;
   the boundary test fails first, and the missing action would otherwise appear as AccessDenied at runtime. A new boundary version
   is created by the IAM administrator; the deployer cannot create one.
9. **The ceiling is evaluated for the nine roles synthesized today.** The test uses placeholder role-name suffixes and
   representative values for imported ARNs.

## 4. API execution permission (`states:StartExecution`)

Finding: the API task role holds `states:StartExecution` on the pipeline state machine. While `PIPELINE_EXECUTION_ENABLED` is off
(startup refuses it) the permission is unused. The reconciler role has no such permission.

Decision: **it can be omitted from the initial effective permissions without changing the CDK role or breaking activation.**

- `runtime-permissions-boundary.json` contains no Allow for `states:StartExecution` and an unconditional Deny, so the statement
  in the API task role is ineffective. A test shows the only uncovered role statement is the API task role's `StartExecution`.
- `cdk diff` is unchanged and no application code changed, so a later activation does not depend on re-adding a CDK statement.
- The reconciler, the Scheduler role, the Step Functions role and every pipeline role are denied under both variants (test).

Activation procedure (a separate, written approval; **not performed**):

1. Complete the pipeline verification and the Lambda trust check (H-3), and approve activation in a future phase that also
   changes the application so that `PIPELINE_EXECUTION_ENABLED=true` is accepted. Today it deliberately stops startup.
2. The IAM administrator creates a new version of `irish-energy-runtime-boundary` from
   `runtime-permissions-boundary-execution-activation.json` and sets it as the default (the deployer cannot).
3. The variant adds one Allow (`states:StartExecution` on `stateMachine:irish-energy-pipeline`, API task role only) and replaces the
   unconditional Deny with a Deny for every principal except the API task role (`ArnNotLike`). A test checks that this is the
   only difference.
4. Review the activation in that phase; reverting is a boundary version change.

An optional later cleanup is to grant the permission in CDK only when an activation context is set; that needs a code change
and was not made here.

## 5. Role creation and modification review

`iam-role-management-policy.json` is attached to the execution role and replaces the former broad statement. All actions below
were checked.

| Action | Decision |
|---|---|
| `iam:CreateRole` | only for the nine approved role-name patterns, and only when `iam:PermissionsBoundary` equals the project boundary; a second statement Denies `CreateRole` on any role when the boundary condition is not met |
| `iam:PutRolePolicy`, `iam:DeleteRolePolicy` | nine patterns only, only when the boundary is attached; Denied on any role without the boundary |
| `iam:AttachRolePolicy` | **Denied** for all roles; no managed policy is ever attached (the synthesized roles have none, test) |
| `iam:DetachRolePolicy`, `iam:PutRolePermissionsBoundary`, `iam:DeleteRolePermissionsBoundary` | not allowed; the last two are explicitly Denied for all roles, so the boundary cannot be changed or removed |
| `iam:CreatePolicy`, `CreatePolicyVersion`, `DeletePolicy`, `DeletePolicyVersion`, `SetDefaultPolicyVersion` | not allowed anywhere; explicitly Denied on the boundary policy for the execution role and the deployer |
| `iam:GetRole`, `GetRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `TagRole`, `UntagRole`, `DeleteRole` | nine patterns only |
| `iam:UpdateAssumeRolePolicy` | **not granted by `iam-role-management-policy.json`** (removed in Phase 9.7.2.2, because CloudFormation needs it only when a trust policy changes in an update). It is granted only by the temporary, separately approved `iam-trust-update-policy.json` (nine patterns, attached for one approved change). IAM has no condition key for trust-policy content (limitation 3 above) |
| `iam:PassRole` | separate policy, nine patterns, `iam:PassedToService` per service; the same patterns as role creation (test) |
| `iam:CreateServiceLinkedRole` | only the ECS and ELB service-linked roles, `iam:AWSServiceName` condition |
| Users, groups, access keys, login profiles, instance profiles | never granted (test) |

Service principals: a role name pattern is bound to exactly one service in `passrole-policy.json`, and a test synthesizes all nine
roles and confirms that each matches exactly one pattern and that its trust policy contains exactly that service
(`lambda`, `scheduler`, `states`, `ecs-tasks`). IAM cannot enforce this at role creation; the test and `cdk diff` do.

The execution role therefore cannot create an unrestricted role: it cannot create any role outside the nine names, and cannot
create one without the boundary.

## 6. Bootstrap and ECR publishing clarified

No bootstrap information was retrieved from AWS; everything below is read from the synthesized manifest and the CDK
documentation.

| Question | Answer |
|---|---|
| Which role publishes Lambda container assets? | the **image-publishing role** `cdk-<qualifier>-image-publishing-role-851725545966-eu-west-1` |
| Which role publishes file assets (templates, the reconciler zip)? | the **file-publishing role** `cdk-<qualifier>-file-publishing-role-851725545966-eu-west-1` |
| Can the deployment identity assume them? | yes: `deployment-identity-policy.json` allows `sts:AssumeRole` and `sts:TagSession` on exactly the deploy, file-publishing and image-publishing roles. Whether the roles' own **trust policy** accepts the deployer is unknown until the bootstrap exists (qualifier `energydev01`) |
| Which repository holds the Lambda image? | `cdk-<qualifier>-container-assets-851725545966-eu-west-1` (tag is the asset content hash, linux/amd64). The API image is separate: `irish-energy-api` |
| Is the bootstrap repository policy sufficient? | **unknown.** AWS documents that bootstrap template version 11 or later adds a policy letting Lambda pull from that repository. If the administrator's read-only check shows that statement, it is sufficient and no extra permission is needed. If it is missing, Lambda tries to add it itself, which needs `ecr:SetRepositoryPolicy`; that action is deliberately **not** granted. The administrator should then add the statement once, or grant `SetRepositoryPolicy` on that one repository for the first deployment only |
| What does the execution role need for the Lambda image? | `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and `ecr:GetRepositoryPolicy` on that repository (in `lambda-deployment-policy.json`) |

## 7. Optional frontend publishing

`frontend-publishing-policy.json`, for a separate role `irish-energy-frontend-publisher`, used only when a reviewed site
publication is approved:

- `s3:ListBucket` and `s3:GetBucketLocation` on `arn:aws:s3:::REPLACE_SITE_BUCKET`;
- `s3:PutObject` and `s3:DeleteObject` on `arn:aws:s3:::REPLACE_SITE_BUCKET/*` (delete is for replacing hashed assets);
- `cloudfront:CreateInvalidation` and `cloudfront:GetInvalidation` on `arn:aws:cloudfront::851725545966:distribution/REPLACE_DISTRIBUTION_ID`.

Resolve the placeholders from the outputs `SiteBucketName` and `DistributionId` of `IrishEnergyFrontendStack` after it exists.
There is no `Resource: *`, no IAM or STS action, no read of object data, and no access to the data lake bucket, any other bucket or
any other distribution; the placeholders are replaced by exact identifiers, never patterns. If a sync tool needs `s3:GetObject`
for comparison, add it on that one bucket only. The site bucket is private with origin access control and a bucket policy, which
this policy does not change.

## 8. How the boundary is applied (not done here)

The application must create its roles with the boundary. The installed CDK supports this through the application context key
`@aws-cdk/core:permissionsBoundary`; it applies to every IAM role in the app, which today means exactly the nine runtime roles. It
would be added to `infrastructure/cdk.json` (shown here only, **not applied**, because it would make every synthesized
template reference a policy that does not yet exist):

```json
{
  "app": "python app.py",
  "context": { "@aws-cdk/core:permissionsBoundary": { "name": "irish-energy-runtime-boundary" } }
}
```

I verified the effect offline: synthesizing all eight stacks with that context gives every one of the nine roles
`PermissionsBoundary` pointing at the boundary, and the default synthesis (without the context) leaves them without one, so the
repository is unchanged.

Order for the administrator: create the boundary policy (IAM administrator, not the deployer) and the six execution policies;
bootstrap with the dedicated qualifier; add the context key; re-synthesize and confirm all nine roles carry the boundary;
deploy stack by stack. Do not bootstrap with `--custom-permissions-boundary` for this boundary.

## 9. Tests and validation (all offline)

New and extended tests cover: boundary presence and project scope; service allow-list; the initial and activation variants;
no `StartExecution` for the reconciler or the scheduler under either variant; no game resources; immutability (explicit Deny, no
Allow); the nine role-name patterns equalling the PassRole patterns; the boundary condition on role creation and inline policies;
only documented IAM actions and no user, policy or managed-policy creation; PassRole service pairing against the synthesized
trust policies; every synthesized role carrying the boundary when the context is set; the boundary covering every synthesized
role statement; and the frontend policy scope. The older package tests still check JSON, the 6,144-character limit, wildcard use,
placeholders, action names against botocore models, and resource names against the templates.

The counts below are **point-in-time results recorded at the end of Phase 9.7.2.1, not current results**; later phases changed them. They come from offline checks against local models and do not establish effective AWS permissions, which are confirmed only during supervised deployment.

| Check | Result |
|---|---|
| Backend and infrastructure tests | 1185 passed, 2 skipped (1153 before; 32 new); point-in-time, end of Phase 9.7.2.1 |
| Ruff | 205 findings (unchanged) |
| Frontend tests, lint, production build | 90 passed; lint clean; build passes |
| Policy JSON syntax and size | 13 documents valid at that time (15 now); largest 5,935 of 6,144 characters; point-in-time |
| Differences under `src/`, `infrastructure/`, `frontend/` | none at that time (one line in `infrastructure/stacks/energy_edge_stacks.py` was changed later, in Phase 9.7.2.2) |

## 10. Remaining risks

- The boundary and role-management conditions (`iam:PermissionsBoundary`, `aws:PrincipalArn` in a boundary) follow the AWS
  documentation but were not exercised in AWS; the first supervised deployment must confirm that role creation succeeds.
- `iam:UpdateAssumeRolePolicy` and trust-policy content cannot be restricted by IAM (limitation 3).
- Resource-based policies naming role ARNs are outside the boundary (limitation 4).
- All Phase 9.7.2 items remain: bootstrap state and qualifier, generated-name patterns, the unverified EC2 and load balancer
  conditions, the Lambda trust condition (H-3) and the Critical findings C-1 to C-3.
- The boundary must be kept in step with any change to a runtime role.
