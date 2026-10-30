# DNS and certificates for Irish Energy Analytics

Status: configuration and documentation only. Nothing here has been deployed, and
no certificate or DNS record has been created. Pipeline execution stays disabled.

Account `851725545966`, region `eu-west-1`, domain `ai-autofy-general-server.com`.

| Purpose | Hostname | Served by |
|---|---|---|
| Energy API | `energy-api.ai-autofy-general-server.com` | HTTPS Application Load Balancer (`IrishEnergyApiEdgeStack`) |
| Energy frontend | `energy.ai-autofy-general-server.com` | CloudFront (`IrishEnergyFrontendStack`) |
| Game API (not ours) | `api.ai-autofy-general-server.com` | the Bluff or Believe game |
| Game frontend (not ours) | `play.ai-autofy-general-server.com` | the Bluff or Believe game |

## Hard rules

- Never edit, replace or delete the game's records (`api.`, `play.`), its ALB,
  its CloudFront distribution or its ACM certificate.
- Do not assume the game's certificate covers `energy-api.`. It almost certainly
  does not, and the energy stacks never reference it. Issue separate certificates.
- The energy project has its own VPC, ALB, bucket, distribution and certificates.
  The CDK code refuses the game's hostnames, the apex, wildcards, any other
  account, and certificates in the wrong region (`infrastructure/energy_config.py`).
- The stacks create **no** certificates and **no** DNS records. The steps below
  are manual and are yours to perform after review.

## 1. ACM certificate for the API (eu-west-1)

An ALB can only use a certificate from its own region.

1. In ACM, region **eu-west-1**, request a public certificate for exactly
   `energy-api.ai-autofy-general-server.com`, with DNS validation.
2. ACM shows one CNAME validation record for that name. Create it in the
   `ai-autofy-general-server.com` hosted zone (see section 3). It is unique to
   this certificate; do not reuse or edit the game's validation records.
3. Wait for the status `Issued`, then note the certificate ARN
   (`arn:aws:acm:eu-west-1:851725545966:certificate/<id>`).

## 2. ACM certificate for the frontend (us-east-1)

CloudFront only accepts certificates from **us-east-1**, whatever region the
stacks use. This is a separate certificate from the API one.

1. In ACM, region **us-east-1**, request a public certificate for exactly
   `energy.ai-autofy-general-server.com`, with DNS validation.
2. Create the CNAME validation record ACM shows (again unique to this certificate).
3. When `Issued`, note the ARN
   (`arn:aws:acm:us-east-1:851725545966:certificate/<id>`).

## 3. Route 53 records

Use the existing public hosted zone for `ai-autofy-general-server.com`. Add
**only** new records; never change existing ones. Check first that these names do
not already exist.

| Record | Type | Value | When |
|---|---|---|---|
| ACM validation for `energy-api.` | CNAME | name and value copied from the eu-west-1 certificate | before issuing |
| ACM validation for `energy.` | CNAME | name and value copied from the us-east-1 certificate | before issuing |
| `energy-api.ai-autofy-general-server.com` | A (and AAAA only if the ALB is dual-stack) alias | the energy ALB: stack outputs `AlbDnsName` and `AlbCanonicalHostedZoneId` | after the API stack deploys |
| `energy.ai-autofy-general-server.com` | A and AAAA alias | the energy distribution: stack output `DistributionDomainName`; alias hosted zone ID is always `Z2FDTNDATAQYW2` | after the frontend stack deploys |

Create the alias records only after the stacks exist, so the names never point at
nothing. Do not touch `api.` or `play.`.

## 4. Synthesize locally (no AWS access)

Without certificates `cdk synth` produces five stacks: `IrishEnergyDataLakeStack`,
`IrishEnergyApiRegistryStack`, `IrishEnergyRunHistoryStack`, `IrishEnergyPipelineStack` and
`IrishEnergyWorkflowStack`. The two edge stacks (`IrishEnergyApiEdgeStack`, `IrishEnergyFrontendStack`) are added
only when both certificate ARNs are supplied, giving seven. The optional reconciler stack makes eight
(`-c reconcilerEnabled=true`; its schedule stays disabled unless `-c reconcilerScheduleState=ENABLED`).
Every stack, the data lake included, is pinned to account `851725545966` and region `eu-west-1`. Synthesizing a stack does not mean the general execution role deploys it: the frontend stack is created only in a supervised administrator operation (section 6).

```bash
cd infrastructure
export CDK_DEFAULT_ACCOUNT=851725545966 CDK_DEFAULT_REGION=eu-west-1
npx cdk synth \
  -c apiCertificateArn=arn:aws:acm:eu-west-1:851725545966:certificate/<api-cert-id> \
  -c frontendCertificateArn=arn:aws:acm:us-east-1:851725545966:certificate/<frontend-cert-id>
```

Synthesis writes templates to `cdk.out`; it does not deploy. Do not commit real
ARNs; pass them on the command line. The code stops with a clear error if one ARN
is missing, a certificate is in the wrong region or account, or the active
account is not exactly `851725545966`. `CDK_DEFAULT_ACCOUNT` must be set explicitly, offline too: a missing, empty or blank value is refused
(checked before any stack is created, so no template is written). Tests run the same synthesis offline with
placeholder ARNs (`tests/test_energy_edge_infrastructure.py`).

## 5. What the stacks create (when deployed later)

The API edge stack is deployed with the general execution role; the frontend stack is created in a supervised administrator operation (section 6).

- `IrishEnergyApiEdgeStack`: its own VPC (public subnets, no NAT), an
  internet-facing ALB with deletion protection and invalid-header dropping, port 80
  redirecting to 443, a TLS 1.3/1.2 HTTPS listener using the eu-west-1 certificate,
  and a host-header rule for `energy-api.` that currently returns 503 until an ECS
  service stack adds a target group. Everything else returns 404.
- `IrishEnergyFrontendStack`: a private, encrypted, HTTPS-only S3 bucket (retained
  on stack deletion), a CloudFront distribution for `energy.` using the us-east-1
  certificate with origin access control, HTTPS redirect, TLS 1.2 minimum,
  security headers, and `index.html` fallbacks for the single-page app.

A load balancer and VPC have a monthly cost whether or not traffic arrives.

## 6. Before any deployment

- [ ] The two certificates are `Issued` in the correct regions and cover only the
      energy names.
- [ ] A human has reviewed `cdk diff` and confirmed it touches only the
      `IrishEnergy*` stacks.
- [ ] The Cognito app client callback and sign-out URLs use
      `https://energy.ai-autofy-general-server.com/`, and the API's
      `CORS_ALLOWED_ORIGINS` is exactly that origin.
- [ ] Permissions for ELB and EC2 (general execution role) are confirmed with an administrator; the developer profile is not widened. CloudFront and the site bucket are **not** in the general execution policy: creating `IrishEnergyFrontendStack` is a supervised administrator operation, and the identity that carries its temporary permissions is an administrator decision to be made first (`docs/phase-9.7.2-administrator-preparation.md`, section 14.1a).
- [ ] The game's records, load balancer, distribution and certificate are unchanged.

## Rollback

Remove the two alias records. Then destroy the API edge stack (`IrishEnergyApiEdgeStack`) with the rollback permissions: the ALB has deletion protection that an approved update must switch off first, and the teardown can stop at the ECS task definition if resource-level deregistration is not honoured (the administrator then deregisters the project revisions in a supervised step). The frontend stack is **not** destroyed together with the edge stack by one routine command: it is deleted only in a supervised administrator session using permissions outside the general package, deleting the distribution first; the retained site bucket is then emptied and deleted separately. Certificates and validation records can then be removed. None of this touches the game.
