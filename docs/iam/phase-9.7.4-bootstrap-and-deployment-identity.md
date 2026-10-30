# Phase 9.7.4: CDK bootstrap and deployment identity security design

Status: **offline design, repository implementation and documentation.** No AWS API, CLI, bootstrap, deployment, IAM or resource change was made, no
credential or key file was read, and nothing is staged or committed. The existing `CDKToolkit` stack and the game are untouched and were not inspected.
Pipeline execution stays disabled; the reconciler schedule stays disabled by default. The bootstrap facts below come from the CDK bootstrap
template printed locally by the installed CLI (2.1146.1, template version 32) and AWS documentation, **not from the AWS account**. The administrator must diff the template of the
CDK CLI in use before applying it, and everything that depends on the account or on AWS behaviour is unverified until separately authorised testing.

## 1. What Phase 9.7.3 found and what changed here

| Finding | Status after this phase |
|---|---|
| B-1 bootstrap environment unknown, sharing undecided | **design decided: option B (dedicated qualifier and toolkit stack, customised template)**; the account state is still an administrator prerequisite |
| B-4 the deployer's Deny statements do not bind a session that assumed the bootstrap deployment role | **design resolved** by replacing the deployment role's permissions and trust in a dedicated bootstrap (sections 4 to 7); not applied anywhere |
| B-5 availability-zone context lookup needs the bootstrap lookup role | **resolved in the repository**: the context is pre-populated in `infrastructure/app.py`; every configuration synthesizes with no missing context (section 9) |

## 2. The actual bootstrap model (template version 32)

| Resource | Trust (who can assume) | Permissions | Consequence for this project |
|---|---|---|---|
| Deployment role `cdk-<q>-deploy-role-…` | **the account root** (any principal in the account that has `sts:AssumeRole` permission on it), `TagSession`, plus optional `TrustedAccounts`; `DenyExternalId` defaults to true | `cloudformation:CreateStack`, `UpdateStack`, `CreateChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet`, `RollbackStack`, `ContinueUpdateRollback`, `DeleteStack`, `UpdateTerminationProtection`, stack refactor actions on **`*`**; `iam:PassRole` only on the execution role; `ssm:GetParameter(s)` on the version parameter; read of the staging bucket; the managed policy `AWSCloudFormationReadOnlyAccess`; cross-account artifact statements | CloudFormation write on every stack in the account, not only this project's. The template comments call it a role that is also meant for the CLI |
| CloudFormation execution role `cdk-<q>-cfn-exec-role-…` | CloudFormation service, restricted by account and the eight named project stack ARNs | the seven managed policies passed with `--cloudformation-execution-policies`; the `irish-energy-deployment-boundary` permissions boundary | this is where project deployment permissions are limited; the boundary applies **only to this role** |
| File publishing role | account root, `TagSession` | read, write and delete on the staging bucket and (if created) its KMS key | writes assets, can delete objects in the bucket |
| Image publishing role | account root, `TagSession` | push, pull and describe on the container repository, `ecr:GetAuthorizationToken` | pushes the pipeline image |
| Lookup role | account root, `TagSession`, optional lookup-trusted accounts | the managed policy **`ReadOnlyAccess`** with a Deny of `kms:Decrypt` | read-only, but broad (it reads data of most services); needed only for context lookups |
| Staging bucket, container repository | resource policies: bucket denies non-TLS; repository allows the Lambda service to pull for `function:*` in the account, and EMR Serverless | | resource-based policies that must be reviewed (section 8) |
| `CdkBootstrapVersion` parameter and stack outputs | | | the CLI checks the version through the deployment role |

Verification of the trust model (from the template, not AWS): the roles delegate to IAM by trusting the account root, so **identity policies decide who may assume them**. The
deployer policy allows only three roles, but it cannot stop another principal in the account that has `sts:AssumeRole` permission, and the trust itself does not name the deployer.

## 3. How role assumption really limits the deployer (the B-4 finding)

1. An explicit Deny in the deployer's identity policy applies to the deployer's own session. After `sts:AssumeRole` the new session carries **only the assumed role's permissions**; the deployer's policy and any permissions boundary on the original identity do not constrain it. A boundary also does not limit the role the CloudFormation service later assumes.
2. The default deployment role can call `DeleteStack` and `UpdateStack` on `*`. A stack that was created with a service role is deleted with that stack's role unless a different one is passed (and passing requires `iam:PassRole`, which the deployment role has only for its own execution role). With a **shared** bootstrap the game's stacks may have been deployed with the shared execution role, which defaults to `AdministratorAccess`: then the deployment role can have those stacks deleted. The code and tests of this repository cannot show whether that is the case; it is an unverified, plausible risk.
3. The default execution role is not constrained by the deployer. It is limited by its own policies. Its default is `AdministratorAccess`, and it can change the bootstrap resources and IAM unless the policies given at bootstrap exclude that.
4. Trust is the account root: restrictions on who may assume the roles exist only in identity policies unless the trust policy is changed.

## 4. Requirements and how the design meets them

| # | Requirement | Design element | Artifact |
|---|---|---|---|
| 1 | The deployer cannot modify or delete the `CDKToolkit` stack | the deployment role's CloudFormation permissions name the eight project stacks only, so no toolkit stack is in scope; the deployer policy keeps its Deny; the execution role is denied too | `bootstrap/deploy-role-policy.json`, `deployment-identity-policy.json`, `bootstrap/cfn-exec-protection-policy.json` |
| 2 | The deployer cannot gain administrator privileges through the deployment role | the deployment role's policy is replaced (no `*` resources for CloudFormation, no managed read-only policy, no cross-account or KMS statements); trust only the deployer | `bootstrap/deploy-role-policy.json`, `bootstrap/operator-roles-trust-policy.json` |
| 3 | The execution role has only what this project needs | the seven managed policies (six project policies and the protection policy) are given with `--cloudformation-execution-policies`; no `AdministratorAccess` | `docs/iam/*.json`, `bootstrap/cfn-exec-protection-policy.json` |
| 4 | The execution role cannot modify bootstrap resources or policies | explicit Denies on `cdk-<q>-*` roles, the asset bucket, the container repository, `/cdk-bootstrap/*` parameters, `CDKToolkit*` stacks and on policy-version changes | `bootstrap/cfn-exec-protection-policy.json` |
| 5 | The execution role cannot delete or modify the game | the project policies use exact ARNs or name prefixes and tag conditions (unverified); a dedicated qualifier means the game's bootstrap resources are not shared; the trust limits the role to project stacks | the six policies, `bootstrap/cfn-exec-trust-policy.json` |
| 6 | The bootstrap roles cannot bypass the runtime boundaries | no bootstrap role receives IAM write actions; the deployment role has `iam:PassRole` only for the execution role; the execution role cannot assume roles | `bootstrap/deploy-role-policy.json`, `bootstrap/cfn-exec-protection-policy.json` |
| 7 | The execution role cannot remove or weaken the project boundaries | Denies of boundary removal and policy-version changes; no Allow for those actions | `iam-role-management-policy.json`, `bootstrap/cfn-exec-protection-policy.json` |
| 8 | The bootstrap trust restricts who can assume the roles | trust names the deployer role with `aws:PrincipalArn`, requires no external ID, no trusted accounts; the execution role trusts only CloudFormation for project stacks | `bootstrap/operator-roles-trust-policy.json`, `bootstrap/cfn-exec-trust-policy.json` |
| 9 | PassRole restricted | execution role: `passrole-policy.json`; deployment role: only the execution role with `iam:PassedToService=cloudformation.amazonaws.com`; deployer: the same | `passrole-policy.json`, `bootstrap/deploy-role-policy.json` |
| 10 | Resource-based policies are accounted for | section 8 | |

## 5. Bootstrap options compared

| | A. Existing bootstrap with restricted trust and policies | B. Dedicated qualifier and toolkit stack (customised template) | C. Dedicated execution role with supervised deployment |
|---|---|---|---|
| Security advantages | no second set of bootstrap resources | every bootstrap role is owned by this project and customised: scoped deployment role, deployer-only trust, no lookup role, limited execution role; the game's roles, bucket and repository are not shared | a project-specific execution role can be limited and used only in supervised sessions |
| Security limitations | the shared stack's trust (account root), the deployment role (`*`), the lookup role and the default administrator execution role cannot be changed without **modifying the existing `CDKToolkit` stack, which is out of scope** and could break the game; the template parameters cannot restrict who assumes the roles | the account root remains the trust principal (IAM delegation) with a condition; the customised template has to be maintained when the CDK is upgraded; the bootstrap administrator is a powerful identity by necessity | the shared deployment role and trust remain (requirement 2 and 8 not met); a custom execution role needs `iam:PassRole` that the shared deployment role does not have; the shared file and image publishing roles keep access to the game's asset bucket and repository; using a separate deployment role needs an application change |
| Required IAM permissions | changes to the shared stack by the game's administrator | bootstrap administrator (`cloudformation`, `iam`, `s3`, `ecr`, `ssm` limited by name to this qualifier and toolkit stack); the six execution policies; the deployer role | a custom execution role plus either application changes (a custom synthesizer with its own role ARNs) or a custom deployment flow |
| Impact on the game | **high**: it changes a stack the game may depend on | **none** by design (different qualifier, toolkit stack name, role, bucket, repository and parameter names) | the shared bootstrap stays as it is, but the shared roles are still used |
| Operational complexity | low to start, high to verify and unsafe to change | moderate: one more bootstrap stack; a maintained template; `cdk.json` qualifier; one-time administrator work | moderate to high: custom flow, supervised sessions, role passing |
| Rollback | cannot be rolled back without touching the shared stack | delete only the project's toolkit stack after the project stacks (termination protection on); the retained bucket and repository are removed by hand | revert the custom role; shared resources are unchanged |
| Possible without changing the existing `CDKToolkit` stack? | **no** | **yes** | yes |
| Possible without changing the application? | yes | needs only a `cdk.json` context (qualifier), applied in the repository | needs an application change (synthesizer properties) |

**Recommendation: B.** A cannot satisfy requirements 2 and 8 without modifying the existing stack. C leaves the shared deployment, file publishing and image publishing roles in use, so it cannot meet requirements 2 and 8 either. If the administrator decides against B, the residual risks of B-4 must be accepted in writing.

## 6. Design of the dedicated bootstrap

Names (decided): qualifier `energydev01` (the longest role name stays within the 64-character IAM limit: `cdk-energydev01-image-publishing-role-851725545966-eu-west-1` is 60), toolkit stack `CDKToolkit-energydev01`, account `851725545966`, region `eu-west-1`. The application uses the qualifier through the context `@aws-cdk/core:bootstrapQualifier` in `infrastructure/cdk.json` and `infrastructure/app.py` (applied; tests check the synthesized manifest and asset files and fail if any stack references `hnb659fds`). The default bootstrap (`hnb659fds`, stack `CDKToolkit`) is not used and must not be modified. All policies in `docs/iam` name the qualifier directly.

The customised template is [bootstrap/energydev01-bootstrap-template.yaml](bootstrap/energydev01-bootstrap-template.yaml). It was derived from the output of `cdk bootstrap --show-template` of the installed CDK CLI (2.1146.1, bootstrap template version 32), which the CLI prints locally; no AWS call was made and the template is **not applied**. Changes against the default template:

| Area | Change | Mirrors |
|---|---|---|
| Deployment, file-publishing and image-publishing roles, trust | the account root with `aws:PrincipalArn` equal to the role named by the parameter `DeployerRoleName` (default `irish-energy-cdk-deployer`) and `sts:ExternalId` null; `TagSession` with the same condition; `TrustedAccounts` unused | `bootstrap/operator-roles-trust-policy.json` |
| Deployment role policy | inline `default` policy replaced; `AWSCloudFormationReadOnlyAccess`, cross-account S3 and KMS statements, `UpdateTerminationProtection` and the stack-refactor actions removed; `DeleteStack` only on the four stateless stacks | `bootstrap/deploy-role-policy.json` |
| Lookup role | resource removed (nothing needs a lookup, section 9) | |
| CloudFormation execution role, trust | the CloudFormation service with `aws:SourceAccount` and `aws:SourceArn` for the eight project stacks | `bootstrap/cfn-exec-trust-policy.json` |
| CloudFormation execution role, policies | the managed policies come only from `CloudFormationExecutionPolicies`; the `AdministratorAccess` fallback is removed (an empty list gives a role with no policies, so deployments fail closed) | seven policies, below |
| Container repository policy | only the Lambda pull statement, limited to `function:irish-energy-*`; the EMR Serverless statement removed | |
| KMS | the key, its alias, the bucket key reference, and the key statement of the file-publishing role removed; the bucket uses SSE-S3 (`AES256`). The deprecated `FileAssetKeyArn` output and its export `CdkBootstrap-${Qualifier}-FileAssetKeyArn` are kept for compatibility and, as in the standard template with `AWS_MANAGED_KEY`, hold the value `AWS_MANAGED_KEY` (also when the parameter is empty, since no key is ever created) | |
| Parameters that could widen the stack | `Qualifier` allows only `energydev01`; `FileAssetsBucketName` and `ContainerAssetsRepositoryName` allow only empty; `FileAssetsBucketKmsKeyId` allows empty or `AWS_MANAGED_KEY` (ignored); `PublicAccessBlockConfiguration`, `DenyExternalId` and `UseExamplePermissionsBoundary` allow only the safe value; `TrustedAccounts` and `TrustedAccountsForLookup` are kept (the CLI passes them) but unused | |
| Example permissions boundary resource | removed (it is an `Allow *` policy) | |
| `BootstrapVariant` | default changed so the CLI will not overwrite a default-variant stack | |
| `InputPermissionsBoundary` | defaults to `irish-energy-deployment-boundary`; the policy was created before bootstrap so the execution role can receive it during bootstrap | |


### Deployment permissions boundary

The customer-managed policy `irish-energy-deployment-boundary` has been created in account `851725545966`.

- ARN: `arn:aws:iam::851725545966:policy/irish-energy-deployment-boundary`
- Default version: `v1`
- Compact policy size: 6,046 characters (AWS limit: 6,144)
- IAM Access Analyzer validation: no findings
- Attachment count: 0 at creation; it is not attached until the dedicated bootstrap is performed.

The boundary limits the CloudFormation execution role to the actions identified in the six project deployment policies. Its main statement uses `Resource: "*"`, so resource-level restrictions continue to depend on those attached policies and their conditions. The seven managed deployment policies, including the separate bootstrap protection policy, remain attached independently.

The seven execution managed policies (passed with `--cloudformation-execution-policies`): the six project policies (`cloudformation-execution`, `lambda-deployment`, `ecs-deployment`, `network-deployment`, `iam-role-management`, `passrole`) and `bootstrap/cfn-exec-protection-policy.json`.

**Template fields that cannot be, or were deliberately not, customised**

- **Termination protection** is not a template property. It is the CLI option `--termination-protection`; the template cannot enforce it.
- **The toolkit stack name** is a CLI argument (`--toolkit-stack-name CDKToolkit-energydev01`), not a template value. A mistake would target the existing stack; the command must never be run without it. The template cannot check it.
- **Parameters the CLI passes** (`TrustedAccounts`, `TrustedAccountsForLookup`, `FileAssetsBucketKmsKeyId`, `BootstrapVariant` and the others) cannot be deleted: CloudFormation rejects parameters the template does not declare. They stay declared; the unused ones are documented as ignored.
- **The file-publishing role's S3 actions** keep the CDK wildcards (`GetObject*`, `GetBucket*`, `List*`, `DeleteObject*`, `PutObject*`, `Abort*`), limited to the project bucket. Narrowing them could break asset publishing; which exact actions the CLI needs is unverified. The image-publishing role and the repository permissions are unchanged defaults.
- **The bootstrap version** (`32`) and the `BootstrapVersion` output stay as the installed CLI produced them. A newer CLI may require a newer version or additional resources.

**Unverified (not testable offline; each fails closed)**: that CloudFormation accepts the template (it was not validated; validation is an AWS API call); that the CLI accepts the customised variant; that CloudFormation supplies `aws:SourceArn` and `aws:SourceAccount` when it assumes the execution role; that `aws:PrincipalArn` matches the deployer's session; that stack-level `Describe*` calls on a not-yet-existing stack are authorised by the `stack/<name>/*` resources; that the deployment role needs nothing that was removed; that the CLI works with SSE-S3 default bucket encryption.

The execution role's permissions boundary parameter (`InputPermissionsBoundary`) applies only to the execution role, not to the deployment role. It now defaults to `irish-energy-deployment-boundary`. The existing application runtime roles continue to use `irish-energy-runtime-boundary`.

Bootstrap command (**REQUIRES EXPLICIT ADMINISTRATOR APPROVAL, not run**):

```bash
cdk bootstrap aws://851725545966/eu-west-1 \
  --qualifier energydev01 --toolkit-stack-name CDKToolkit-energydev01 \
  --template docs/iam/bootstrap/energydev01-bootstrap-template.yaml \
  --cloudformation-execution-policies "<seven policy ARNs>" \
  --termination-protection
```

Never run it without `--qualifier`, `--toolkit-stack-name` and `--template`. `--no-bootstrap-customer-key` is not needed (the template never creates a key).

Sequence: (1) IAM administrator creates the boundary, the seven managed policies and the deployer role; (2) the administrator re-prints `cdk bootstrap --show-template` with the CLI in use and diffs it against the template (a newer CLI may differ); (3) bootstrap with the reviewed template; (4) read-only verification (trust, inline policy and attached policies match these documents, parameter value, no lookup role); (5) a supervised first deployment of the smallest stack.

## 7. Effective permissions after the design (and its limits)

- **Deployer:** can assume only the three roles that trust it, and those roles can change only the project stacks. The Deny statements in `deployment-identity-policy.json` stay as a second layer but are not the control.
- **Deployment role:** CloudFormation write on eight stacks (delete on four), pass the execution role only. It cannot read other stacks, change IAM, or touch S3 outside the project bucket reads. It cannot use a different role. Deleting a game stack is not in its scope; whether CloudFormation would nonetheless act on a game stack is not possible because the deployment role has no permission on it.
- **Execution role:** the project policies minus the Denies in the protection policy; it cannot assume roles, create users or keys, or change bootstrap, boundary or execution policies.
- **Residual:** the account root still delegates to IAM. Any principal with `iam:UpdateAssumeRolePolicy` or administrator rights can change trust; the bootstrap administrator can always change the toolkit stack. Those identities are outside the deployer and are controlled by account governance, not by this design. The deployment role's actions on changesets and the exact resource types are unverified and fail closed.

## 8. Resource-based policies and the bypass question

| Resource-based policy | Who it grants | Bypass risk | Control in the design |
|---|---|---|---|
| Role trust policies of the bootstrap roles | default: the account root | any principal with `sts:AssumeRole` permission can use the role | trust restricted to the deployer role (`aws:PrincipalArn`); no `TrustedAccounts`; no external ID |
| Execution role trust | the CloudFormation service | any CloudFormation stack that a caller can pass the role to (needs `iam:PassRole`) | `aws:SourceArn` and `aws:SourceAccount` limited to project stacks (unverified); `PassRole` only for the deployment role and deployer |
| Asset bucket policy | denies non-TLS only | an identity with S3 permissions in the account can access it | the execution role is explicitly denied writes; the deployment role reads only |
| Container repository policy | the Lambda service for `function:*` | any Lambda function in the account can pull project images | restricted to `function:irish-energy-*`; EMR statement removed |
| Project resources' policies (data lake bucket policy, `irish-energy-api` repository policy, function policies, table policy) | unknown for the pre-existing bucket | a grant that names a role ARN can bypass a boundary | read-only inspection before deployment (existing checklist) |
| KMS key policy | none: the template creates no key (SSE-S3) | | template without KMS resources |

A permissions boundary on the original deployment identity does **not** automatically constrain a role that identity assumes (section 3), and the project's runtime boundary does not constrain the execution role. The design therefore controls the assumed roles directly.

## 9. Availability-zone lookup (B-5) resolved

The edge stack's VPC read the availability zones from the account at synthesis, which needs the lookup role (broad read-only). The manifest of the full configuration listed one missing-context entry (`availability-zones`) with the lookup role ARN. Passing explicit zones to the VPC did **not** remove the lookup (a scratch synthesis showed the VPC still queries the stack's zones), so the fix pre-populates the context key in `infrastructure/app.py` (`availability-zones:account=851725545966:region=eu-west-1` with `eu-west-1a`, `eu-west-1b`, `eu-west-1c`; the VPC still uses two). Result: the manifest of all configurations has no missing context, the edge subnets are in `eu-west-1a` and `eu-west-1b`, the lookup role is not needed, and the deployer's three-role policy is sufficient. Tests assert this. The zone names are assumed valid for the account (an administrator check: `ec2 describe-availability-zones` is a read-only call, not run here).

## 10. Unverified items and remaining blockers

Unverified (each fails closed): the bootstrap template version and differences with the installed CLI; resource-level scope of change-set actions; whether the CloudFormation service supplies `aws:SourceArn` and `aws:SourceAccount` on service-role assumption; `aws:PrincipalArn` matching for the deployer's session; whether the CLI needs actions that were removed from the deployment role; the state of the account's existing bootstrap and the game; the supervised deployment of the first stack. Remaining administrator work: decisions in section 6, creation of the IAM objects, the reviewed template, the bootstrap run, verification. Phase 9.7.3 blockers B-1 (decision made, account state still unknown), B-2, B-3, B-4 (design, not applied), B-6 to B-10 remain; B-5 is resolved. Offline tests inspect these documents and the synthesized output; they do not prove effective AWS permissions.
