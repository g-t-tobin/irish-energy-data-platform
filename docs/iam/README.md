# IAM policy package for the AWS administrator (Phase 9.7.2, hardened in 9.7.2.1)
## Current AWS status — 30 October 2026

**This section supersedes the original status statements below where they
describe whether IAM resources have been created.** The policy analysis and
limitations remain useful, but some of the original preparation statements
are historical.

### Created in AWS

- `irish-energy-runtime-boundary` — created, not attached to runtime roles.
- `irish-energy-deployment-boundary` — created, not attached to the
  CloudFormation execution role.
- Seven managed deployment policies — created, not attached to the
  CloudFormation execution role.
- IAM user `irish-energy-deployer-operator` — created with restricted
  permission to assume the deployment role.
- IAM role `irish-energy-cdk-deployer` — created with its deployment
  permissions and restricted trust relationship.

The deployment boundary passed IAM Access Analyzer validation with no
findings. This is policy validation, not proof of effective permissions
or successful deployment.

### Prepared but not created in AWS

- Custom bootstrap template:
  `bootstrap/energydev01-bootstrap-template.yaml`.
- Dedicated bootstrap stack: `CDKToolkit-energydev01`.
- CloudFormation execution role and its seven managed policy attachments.
- Any application stacks for this deployment.

Neither `cdk bootstrap` nor `cdk deploy` has been run for this project.
The existing default `CDKToolkit` and the separate game infrastructure
must remain untouched.

### Remaining policy placeholders

The API and frontend ACM certificate identifiers are now known. The
hosted-zone identifier is known. The Cognito user-pool identifier and
frontend resource identifiers remain unresolved where referenced by the
optional inspection and frontend policies.

Policies containing unresolved `REPLACE_*` values are templates only.
Do not attach or use them until their identifiers have been resolved
and the policy has been reviewed.

### Deployment safeguards

- The runtime boundary explicitly denies `states:StartExecution`.
- The separate execution-activation boundary has not been created or applied.
- Pipeline execution remains disabled by default.
- The reconciler schedule remains disabled by default.
- No AWS deployment or bootstrap operation is authorised by this
  documentation update.

**Current status: IAM preparation completed in part; dedicated bootstrap
and application deployment remain outstanding.**

---

> **Historical policy documentation:** Sections below retain the original
> policy-package review. Where they say that nothing has been applied or
> created, those statements describe the earlier review baseline, not the
> current AWS state.


**Original Phase 9.7.2 status (historical):** At the time of that review,
nothing in this policy package had been applied. No role, policy, bootstrap
stack or other AWS resource had been created or changed. The existing developer identity (`irish-energy-data-platform-dev`) is not
touched and receives no deployment permissions. Pipeline execution stays disabled and the reconciler schedule stays
disabled by default.

Account `851725545966`, region `eu-west-1`. Read `docs/phase-9.7.2-administrator-preparation.md` first: it explains the
identity design, bootstrap strategy, PassRole analysis, rollback and the read-only checklist. These files are proposals for
human review. They were checked offline against the synthesized CDK templates (section 5), not against AWS. Offline checks do not establish effective AWS permissions; that is confirmed only during supervised deployment.

## 1. Identities

| Identity | Who | Existing or proposed | Policy files |
|---|---|---|---|
| Developer | `irish-energy-data-platform-dev` | existing, **unchanged** | none (keeps its restricted access) |
| CDK deployer | the human administrator running `cdk deploy` | proposed role `irish-energy-cdk-deployer` | `deployment-identity-policy.json`, `ecr-publishing-policy.json`, optional `rollback-policy.json` |
| CloudFormation execution role | assumed by CloudFormation | the bootstrap role `cdk-<qualifier>-cfn-exec-role-851725545966-eu-west-1` | `cloudformation-execution-policy.json`, `lambda-deployment-policy.json`, `ecs-deployment-policy.json`, `network-deployment-policy.json`, `iam-role-management-policy.json`, `passrole-policy.json` |
| Read-only verifier | the administrator, before and after deployment | proposed role `irish-energy-readonly-verifier` (or the deployer) | `read-only-verification-policy.json` |
| Runtime roles | Lambda, ECS, Step Functions, Scheduler | defined by CDK, **unchanged** | none; `irish-energy-runtime-boundary` caps them (section 6) |
| Frontend publisher (optional) | whoever uploads the built site | proposed role `irish-energy-frontend-publisher` | `frontend-publishing-policy.json` |

The CloudFormation execution permissions are split over six documents because an IAM managed policy is limited to 6,144
non-whitespace characters and because separate documents are easier to review. Attach all six to the execution role. The
bootstrap command that wires them in is in the phase document (section 4).

## 2. Placeholders

Eight placeholders are used. The CDK bootstrap qualifier is not a placeholder: every policy uses the dedicated qualifier `energydev01` (account `851725545966`, region `eu-west-1`). `REPLACE_SITE_BUCKET`, `REPLACE_DISTRIBUTION_ID`, `REPLACE_OAC_ID` and `REPLACE_RESPONSE_HEADERS_POLICY_ID` appear only in the optional frontend policies. The first two are the `SiteBucketName` and `DistributionId` outputs of `IrishEnergyFrontendStack`; the other two are read from the created resources. All four exist only after the supervised first frontend deployment. `REPLACE_USER_POOL_ID`, `REPLACE_HOSTED_ZONE_ID`, `REPLACE_API_CERT_ID` and `REPLACE_FRONTEND_CERT_ID` (read-only policy; the first two also in the inspection policies) are the identifiers of the energy user pool, the hosted zone and the two certificates, and are not yet known. The synthesized manifests reference
the qualifier `energydev01` (set in `infrastructure/cdk.json` and `infrastructure/app.py`) and the toolkit stack is
`CDKToolkit-energydev01`; the default bootstrap (`hnb659fds`, stack `CDKToolkit`) is not used and must not be modified. Everything else (account,
region, resource names) comes from the CDK code. No ARN is invented: resources whose names CloudFormation generates at
creation (roles, ECS cluster, ALB, target group, CloudFront IDs, the site bucket) are matched by the documented naming
pattern and listed as unresolved identifiers in section 4.

## 3. Policy index

Classification: **deployment-time** = used only while deploying or verifying; **runtime** = used by running workloads (none of
these documents grant runtime permissions).

| File | Purpose | Principal | Actions (summary) | Resource scope | Conditions | Class | Unavoidable wildcards | Approval needed |
|---|---|---|---|---|---|---|---|---|
| `deployment-identity-policy.json` | What the human deployer may do (now also denies any change to the boundary policy or to role boundaries) | `irish-energy-cdk-deployer` | `sts:AssumeRole` and `sts:TagSession` for the bootstrap deploy, file-publishing and image-publishing roles; read-only CloudFormation monitoring; `iam:PassRole` for the execution role only; explicit Deny of any change to `CDKToolkit*` stacks | the three bootstrap role ARNs; the eight `IrishEnergy*` stack ARNs; the execution role ARN | `iam:PassedToService = cloudformation.amazonaws.com` | deployment-time | `sts:GetCallerIdentity` has no resource-level support | administrator and project owner |
| `cloudformation-execution-policy.json` | Create and update the data, registry, run-history, workflow and schedule resources. **No CloudFront and no site-bucket permissions** (Phase 9.7.2.2) | CloudFormation execution role | Glue and Athena (create, get, update; no delete), ECR (no delete), DynamoDB (no delete), Step Functions, Scheduler | exact ARNs | none | deployment-time | none | administrator, security reviewer |
| `iam-role-management-policy.json` | Create and delete only the nine project runtime roles, only with the boundary attached | CloudFormation execution role | `iam:CreateRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, read, tag, `iam:DeleteRole`; explicit Deny of managed-policy attachment, boundary changes and boundary-less role writes. **No `iam:UpdateAssumeRolePolicy`** (Phase 9.7.2.2) | the nine role-name patterns; the boundary policy ARN | `iam:PermissionsBoundary` equals the boundary ARN on create and inline policy writes | deployment-time | none; role names carry a random suffix | administrator, security reviewer |
| `runtime-permissions-boundary.json` | Ceiling for every runtime role (section 6) | attached to the nine runtime roles as their permissions boundary | the exact actions the roles use today, nothing else; explicit Deny of `states:StartExecution` | project resources only | `aws:PrincipalArn` per role on the sensitive statements | runtime ceiling | `ecr:GetAuthorizationToken` | administrator creates it, not the deployer |
| `runtime-permissions-boundary-execution-activation.json` | Replacement boundary version used only when pipeline execution is separately approved | same | initial boundary plus `states:StartExecution` for the API task role only | as above | `aws:PrincipalArn` | runtime ceiling | as above | project owner, separately; do not apply now |
| `frontend-publishing-policy.json` (optional) | Upload the built site and invalidate the cache | `irish-energy-frontend-publisher` | `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetBucketLocation`, `cloudfront:CreateInvalidation`, `cloudfront:GetInvalidation` | the site bucket and one distribution (placeholders) | none | deployment-time | none | project owner, per publication |
| `frontend-stack-update-policy.json` (optional, supervised) | Update the **existing** frontend stack resources after the first supervised deployment | execution role, attached temporarily for one approved update | `Get`/`Update` of the distribution, origin access control, response headers policy, and site bucket settings; no create, no delete | exact identifiers (placeholders `REPLACE_DISTRIBUTION_ID`, `REPLACE_OAC_ID`, `REPLACE_RESPONSE_HEADERS_POLICY_ID`, `REPLACE_SITE_BUCKET`) | none | deployment-time | none | project owner, per update |
| `iam-trust-update-policy.json` (optional, temporary) | Change the trust policy of a runtime role | execution role, attached only for one approved change | `iam:UpdateAssumeRolePolicy` | the nine role-name patterns | none available in IAM | deployment-time | trust-policy content cannot be restricted by IAM | project owner and security reviewer, every time |
| `lambda-deployment-policy.json` | Create the five Lambda functions and their log groups | CloudFormation execution role | Lambda create, update, delete, tag; pull access to the bootstrap image repository; log group management | the five exact function ARNs and five exact log group ARNs; the bootstrap container repository | none | deployment-time | `logs:DescribeLogGroups` uses `Resource: "*"`: a read-only account-wide listing, because no per-group authorization is documented | administrator |
| `ecs-deployment-policy.json` | Create the ECS cluster and API service | CloudFormation execution role | ECS cluster and service, task definitions, API log group, service-linked roles | cluster and service ARNs by stack-name prefix; log group by prefix | `iam:AWSServiceName` on service-linked roles | deployment-time | `ecs:RegisterTaskDefinition` and `ecs:DescribeTaskDefinition` have no resource-level support (`Resource: "*"`); `ecs:DeregisterTaskDefinition` is limited to the project task-definition family, and resource-level support for it is unverified | administrator |
| `network-deployment-policy.json` (extra, split for size) | VPC, subnets, security groups and the ALB of the edge stack | CloudFormation execution role | EC2 networking create and manage, ELBv2 create and manage, describe | load balancer, listener and rule ARNs name the fixed load balancer `irish-energy-api`; target groups and EC2 resources are isolated by the `Project` tag | `aws:RequestTag/Project` on create; `aws:ResourceTag/Project` on manage and delete; `ec2:CreateAction` on tagging | deployment-time | EC2 and ELB describe actions (read-only metadata); target group and EC2 patterns depend on tags | administrator, network reviewer |
| `ecr-publishing-policy.json` | Push the API image | `irish-energy-cdk-deployer` | `ecr:GetAuthorizationToken`; layer upload and `ecr:PutImage`; image describe | `repository/irish-energy-api` only | none | deployment-time | `ecr:GetAuthorizationToken` is account-wide by design | administrator |
| `passrole-policy.json` | Let CloudFormation pass the project's runtime roles to their services | CloudFormation execution role | `iam:PassRole` | project role-name patterns per stack, never `*` | `iam:PassedToService` per service (`lambda`, `ecs-tasks`, `states`, `scheduler`) | deployment-time | role names have a generated suffix, so a stack prefix pattern replaces an exact ARN | administrator, security reviewer |
| `read-only-verification-policy.json` | Routine, low-exposure verification before and after deployment | `irish-energy-readonly-verifier` | an exact reviewed allow-list of `Describe`, `Get` and `List` calls (see section 10); `ssm:GetParameter` on the exact bootstrap version parameter; `lambda:GetPolicy` on the five project functions; `route53:GetHostedZone` on the energy zone. **Moved to separate policies:** `route53:ListResourceRecordSets`, `cloudformation:DescribeStackEvents`, `cognito-idp:DescribeUserPool`. **Removed:** `cognito-idp:ListUserPools`, `cognito-idp:DescribeUserPoolClient`, `lambda:GetFunction`, `lambda:GetFunctionConfiguration`, `ssm:GetParametersByPath`, `ssm:DescribeParameters`, CloudFront, `iam:ListRoles`, `cloudformation:ListExports` | bootstrap and project resources; the energy user pool, hosted zone and two certificates by placeholder (`REPLACE_USER_POOL_ID`, `REPLACE_HOSTED_ZONE_ID`, `REPLACE_API_CERT_ID`, `REPLACE_FRONTEND_CERT_ID`) | none | deployment-time | lists of names and generic metadata (`ListCertificates`, `ListHostedZonesByName`, `ListStacks`, state machine, cluster, log group, load balancer and VPC lists) | administrator |
| `dns-inspection-policy.json` (optional, needs explicit administrator approval) | List the records of the energy hosted zone | `irish-energy-inspector` (separate from the verifier), attached for one approved inspection | `route53:ListResourceRecordSets` only | the energy hosted zone (`REPLACE_HOSTED_ZONE_ID`, not yet known) | none available: IAM cannot restrict the call to individual record names | deployment-time | returns **every record in the shared zone**, including the game's, and any TXT values | administrator, each time |
| `cloudformation-diagnostic-policy.json` (optional, needs explicit administrator approval) | Read stack events when a stack operation fails | `irish-energy-inspector` | `cloudformation:DescribeStackEvents` only | the eight project stack ARNs (no `CDKToolkit*`) | none | deployment-time | events carry resource properties and status messages that can include configuration values; IAM cannot filter them | administrator, each time |
| `cognito-inspection-policy.json` (optional, needs explicit administrator approval) | Read the energy user pool configuration | `irish-energy-inspector` | `cognito-idp:DescribeUserPool` only | the energy user pool (`REPLACE_USER_POOL_ID`) | none | deployment-time | can reveal SMS configuration (including the SMS role and external ID) and Lambda trigger settings; no client secret | administrator, each time |
| `rollback-policy.json` (optional) | Cancel, roll back or delete a failed stateless stack | `irish-energy-cdk-deployer` | `CancelUpdateStack`, `ContinueUpdateRollback`, `RollbackStack`, `DeleteStack` on four stacks; explicit Deny of `DeleteStack` for the three stateful stacks, the frontend stack and `CDKToolkit*` (the deployer policy also denies any change to `CDKToolkit*` stacks) | the four stateless stack ARNs (Pipeline, Workflow, Reconciler, ApiEdge); the Deny names the stateful, frontend and `CDKToolkit*` stacks, and `CDKToolkit*` may be the game's bootstrap stack if its name is shared | none | deployment-time | none | project owner, on each use |

## 4. Unresolved identifiers and unverifiable assumptions

The administrator must resolve or accept each item before attaching a policy.

1. **Bootstrap:** the dedicated qualifier `energydev01` and toolkit stack `CDKToolkit-energydev01` are decided; the bootstrap itself (customised template `docs/iam/bootstrap/energydev01-bootstrap-template.yaml`) is an administrator action that has not been performed.
2. **Generated names:** role names (`IrishEnergy<Stack>-<LogicalId>-<suffix>`), ECS cluster and service, ALB, target group,
   CloudFront distribution, OAC and response-headers policy IDs, the site bucket (`irishenergyfrontendstack-sitebucket...`)
   and the API log group. They are matched by prefix. If a later change sets explicit names in the CDK code, the patterns can
   become exact ARNs.
3. **Prefix match on truncated names:** CloudFormation shortens generated role names to 64 characters. The patterns match the
   stack-name and logical-id prefix, which truncation keeps for every role synthesized today. Confirm against the first
   created role names.
4. **Action completeness:** the action lists come from the resource types in the synthesized templates and the service
   authorization documentation. CloudFormation's resource handlers sometimes call extra read or tag actions that cannot be seen
   offline. A missing action fails the stack operation with an access-denied event in the stack events and rolls back safely.
   Add only the denied action, narrowly, after review. Never replace these policies with `AdministratorAccess` or `*`.
5. **AWS-dependent assumptions not verified (each fails closed: an AccessDenied in a supervised deployment, never a wider grant).** Confirm
   all of them during the supervised deployments; do not widen a pattern to make a step succeed:
   - the `aws:RequestTag/Project` and `aws:ResourceTag/Project` conditions for EC2 and for target groups, and the `ec2:CreateAction` tagging condition;
   - **fixed-name load-balancer scoping**: whether `CreateLoadBalancer`, `CreateListener`, listener and rule actions are authorised against ARNs that name
     `irish-energy-api`;
   - **ECS task-definition deregistration**: whether `ecs:DeregisterTaskDefinition` honours resource-level scoping to the project family;
   - the exact `ssm:GetParameter` ARN for the bootstrap version parameter (`parameter/cdk-bootstrap/<qualifier>/version`);
   - **IAM permissions-boundary and `aws:PrincipalArn` behaviour**: the `iam:PermissionsBoundary` condition on `CreateRole`, `PutRolePolicy` and
     `DeleteRolePolicy`, the explicit Deny on roles without the boundary, and `aws:PrincipalArn` matching in boundary statements for assumed-role sessions;
   - the resource-level scope of `states:CreateStateMachine`;
   - whether `logs:DescribeLogGroups` with `Resource: "*"` is needed and sufficient for the log group resources;
   - the `ssm`, ACM, Cognito and Route 53 placeholders resolve to the right identifiers.
6. **IAM role creation (reduced in Phase 9.7.2.1):** the execution role can create only the nine approved roles and only with the
   permissions boundary attached; the boundary caps what any of them can ever do (section 6). The boundary does not constrain
   the execution role itself, and IAM cannot restrict the trust-policy principal at `iam:CreateRole`; `cdk diff` review of IAM
   changes remains required. The boundary only takes effect once the application sets it (a `cdk.json` context change that is
   **not** made here, see the phase 9.7.2.1 report) and the administrator has created the boundary policy.
7. **ACM and Cognito:** CloudFormation is not expected to need ACM or Cognito permissions to attach an existing certificate
   or to pass Cognito identifiers as plain values. This was not verified; confirm on the first deployment.
8. **Lambda image pull:** the Lambda policy allows `ecr:GetRepositoryPolicy` but not `ecr:SetRepositoryPolicy`. Bootstrap
   template version 11 or later already allows Lambda to pull from the bootstrap repository. Confirm with
   `aws ecr get-repository-policy` (read-only). Add `ecr:SetRepositoryPolicy` on that one repository only if the statement is
   missing, and only for the first deployment.

## 5. Offline validation

Five test files check the package without any AWS call. They use local models of IAM matching, the synthesized templates and
**invented** game ARNs (the game's real resources have not been inspected). Pattern-level tests show how the documents are written,
not what AWS will authorise: **effective AWS authorization is confirmed only during supervised deployment.**

| File | What it checks |
|---|---|
| `tests/test_iam_policy_package.py` | JSON syntax and structure; the 6,144-character limit; no wildcard action; `Resource: *` only for documented cases; placeholders; project account and region; action names against botocore models; no `states:StartExecution`, Lambda invocation, data-plane, secret, key or user grants in the deployment policies; `sts:AssumeRole` limited to three bootstrap roles; `iam:PassRole` conditions; the ECR policy scope; resource names against the synthesized templates |
| `tests/test_iam_boundary.py` | the boundary and its activation variant; boundary protection in the policy documents; the nine role-name patterns and the role-creation conditions; PassRole and trust-policy service pairing against synthesized roles; every synthesized role statement against the boundary using a local matching model |
| `tests/test_iam_isolation.py` | CloudFront and ELB scoping; EC2 tag conditions; trust-policy permissions; a pattern check against invented game ARNs; wildcard-resource audit; `states:StartExecution` for each of the nine roles; documentation statements |
| `tests/test_iam_readonly_secrets.py` | no client-secret or other sensitive read permission; the exact read-only allow-list; Glue and Athena deletion; documentation statements |
| `tests/test_iam_inspection_policies.py` | the general policy lacks `route53:ListResourceRecordSets`, `cloudformation:DescribeStackEvents`, `cognito-idp:DescribeUserPool` and `cognito-idp:ListUserPools`; each inspection policy has one read action and an exact scope; no DNS, CloudFormation or Cognito write permission anywhere in the package; no content or secret reads; the exact bootstrap parameter permission; JSON validity and size; the documentation statements |

## 6. Permissions boundary (Phase 9.7.2.1)

`runtime-permissions-boundary.json` defines the customer managed policy `irish-energy-runtime-boundary`, created and owned by
the IAM administrator, never by the deployer or the execution role. It is the permissions boundary of all nine runtime roles.
Full design, limits and activation procedure: `docs/phase-9.7.2.1-boundary-hardening.md`.

## 7. Isolation review (Phase 9.7.2.2)

CloudFront and the site bucket were removed from the general execution policy, listener and rule permissions now name the fixed
load balancer, trust-policy updates moved to a temporary policy, and several wildcard reads were narrowed. Findings, the
action-by-action review of CloudFront, ELB, EC2, ECS, CloudWatch Logs and IAM, the resource-based policy bypass list and the
supervised frontend procedure are in `docs/phase-9.7.2-administrator-preparation.md`, section 14. Review every runtime-role
trust-policy change by hand; the boundary does not protect trust-policy contents. The energy app client is inspected separately, never through the general read-only policy (section 14.7).

## 8. Deletion, bootstrap inspection and supervised frontend creation (Phase 9.7.2.3)

- **Deletion of stateful stacks is not granted, by policy.** The documents do not grant deletion of `IrishEnergyDataLakeStack`,
  `IrishEnergyApiRegistryStack` or `IrishEnergyRunHistoryStack`, and `rollback-policy.json` explicitly denies it. This is a
  deliberate policy choice, not something AWS makes technically impossible; a separately approved permission could allow it.
  Deleting them, cleaning up after a failed first creation, and retained-resource name collisions on redeploy are described in
  `docs/phase-9.7.2-administrator-preparation.md`, section 9.1, and need separate approval.
- **Read-only inspection includes bootstrap metadata.** The read-only policy can describe `CDKToolkit*` stacks and read the
  roles and role policies matching `cdk-<qualifier>-*-role-851725545966-eu-west-1`, the bootstrap asset bucket settings, the bootstrap
  repository and its policy. If the qualifier is shared with the game (for example the default), this includes the game's bootstrap
  metadata. It is inspection only: nothing writes, and the deployer policy explicitly denies any change to `CDKToolkit*` stacks.
- **Frontend creation is a supervised administrator operation.** Which identity carries its temporary permissions (the execution
  role widened for the session, or a separate CloudFormation service role) is an unresolved administrator decision; see section 14.1a
  of the phase document.

## 9. Inspecting Lambda configuration and resource policies without exposing secrets

- Resource policies: `aws lambda get-policy --function-name <name> --query Policy --output text` for each of the five project functions, with the general
  read-only identity. The output is a policy document, not a secret.
- Configuration (including environment variable values) is not readable with the general policy; `lambda:GetFunction` and
  `lambda:GetFunctionConfiguration` are not granted. Verify it from the reviewed `cdk diff` and the synthesized templates. If a post-deployment check is
  needed, a separately authorised identity chosen by the administrator reads one function with `aws lambda get-function-configuration` and a `--query`
  that selects only non-secret fields. `--query` is not a security boundary: do not redirect, log or record the response, and prefer the console.
- Never use `aws lambda get-function`: it returns a presigned URL for the deployment package and the environment values.
- The general policy cannot confirm that a function name is unused; the first CloudFormation creation fails with an already-exists error and overwrites
  nothing. Details: `docs/phase-9.7.2-administrator-preparation.md`, section 10.1 (and the review of every general verification permission in section 14.10).

## 11. Deployment readiness audit

`docs/iam/phase-9.7.3-deployment-readiness.md` (Phase 9.7.3) is the deployment-readiness audit and runbook: stack dependencies and order, bootstrap, connectivity, findings classified BLOCKER to LOW, and the overall status **READY FOR ADMINISTRATOR PREPARATION** (not ready for a controlled first deployment).

`docs/iam/phase-9.7.4-bootstrap-and-deployment-identity.md` (Phase 9.7.4) compares three bootstrap options, recommends a dedicated qualifier and toolkit stack (option B) and describes the replacement bootstrap policies in `docs/iam/bootstrap/` (deployment role policy, execution role protection policy, trust policies). They are design artifacts and are not applied by this repository; the availability-zone lookup is resolved in `infrastructure/app.py`.

## 10. Separate inspection policies (Phase 9.7.2.3)

Three read permissions were taken out of the general verification policy because their output can reveal more than routine verification needs.
Each is its own document, attached to a separate role (`irish-energy-inspector`) only for one approved inspection and detached afterwards:

| Policy | Why it is separate | Information exposed |
|---|---|---|
| `dns-inspection-policy.json` | `route53:ListResourceRecordSets` cannot be limited to individual record names, and the zone is shared with the game | every record in the shared hosted zone, including the game's records and any TXT values |
| `cloudformation-diagnostic-policy.json` | stack events include resource properties and status reasons; routine status checks use `DescribeStacks` and `DescribeStackResources` | resource details and configuration values; IAM does not prevent sensitive values from appearing in event messages |
| `cognito-inspection-policy.json` | the pool description is not needed to confirm groups and client names | SMS configuration and Lambda trigger settings; no client secret (that API is not granted) |

**Read-only is not the same as safe.** Each policy needs explicit administrator approval before attachment, a named purpose and detachment afterwards. The general
policy also drops `cognito-idp:ListUserPools` (its per-pool summary can include trigger configuration of other applications' pools); the pool ID comes from the owner's
creation record. The deployer keeps `cloudformation:DescribeStackEvents` on the project stacks to monitor deployments. How to inspect: read the JSON before attaching, run
only the single matching command for the single named resource, never save or paste the output, and detach. The placeholders `REPLACE_HOSTED_ZONE_ID` and
`REPLACE_USER_POOL_ID` are unresolved and nothing here invents them. Resource-level scoping, the response contents and the condition behavior remain **unverified until separately authorised testing**.
