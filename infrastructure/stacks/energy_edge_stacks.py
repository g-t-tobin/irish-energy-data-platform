"""Independent edge stacks for Irish Energy Analytics (API ALB and frontend CloudFront).

Both stacks create their own VPC, ALB, bucket and distribution and only
*reference* certificates by ARN. They create no certificates and no Route 53
records; see docs/deployment-dns-and-certificates.md for those manual steps.
Nothing here refers to the Bluff or Believe game's resources.
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Tags
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct
from energy_config import ApiServiceConfig, EdgeConfig

from src import run_history_schema as schema
from stacks.energy_api_service import ApiService

PROJECT_TAG = "irish-energy-data-platform"


class EnergyApiEdgeStack(Stack):
    """HTTPS Application Load Balancer for the energy API hostname.

    Without a service config, unmatched requests get 404 and the API hostname
    gets a fixed 503. With one (and the registry's repository), the hostname is
    forwarded to the ECS Fargate API service defined in energy_api_service.py.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EdgeConfig,
        service: ApiServiceConfig | None = None,
        repository: ecr.IRepository | None = None,
        table: dynamodb.ITable | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if len({service is None, repository is None, table is None}) != 1:
            raise ValueError("service, repository and table must be provided together")
        Tags.of(self).add("Project", PROJECT_TAG)
        Tags.of(self).add("Component", "api-edge")

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                )
            ],
        )
        alb_security_group = ec2.SecurityGroup(
            self, "AlbSecurityGroup", vpc=vpc, description="Energy API ALB", allow_all_outbound=False
        )
        alb_security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS")
        alb_security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP redirect")

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            # A fixed name lets IAM scope listener and rule permissions to this load balancer (docs/iam).
            load_balancer_name="irish-energy-api",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            drop_invalid_header_fields=True,
            deletion_protection=True,
        )
        alb.add_listener(
            "HttpRedirect",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            default_action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True
            ),
        )
        https = alb.add_listener(
            "Https",
            port=443,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificates=[elbv2.ListenerCertificate.from_arn(config.api_certificate_arn)],
            ssl_policy=elbv2.SslPolicy.RECOMMENDED_TLS,
            default_action=elbv2.ListenerAction.fixed_response(
                404, content_type="text/plain", message_body="Not found"
            ),
        )
        if service is None or repository is None or table is None:
            https.add_action(
                "EnergyApiHost",
                priority=10,
                conditions=[elbv2.ListenerCondition.host_headers([config.api_hostname])],
                action=elbv2.ListenerAction.fixed_response(
                    503, content_type="text/plain", message_body="Service not deployed yet"
                ),
            )
        else:
            # Keeps S3 traffic (Gold reads, image layers) on the AWS network at no cost.
            vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
            # Run-history traffic stays on the AWS network (no hourly charge, no NAT). It serves
            # the route tables of the public subnets the ECS tasks run in. The endpoint policy
            # only narrows access further; the task role still decides what is allowed.
            dynamodb_endpoint = vpc.add_gateway_endpoint(
                "DynamoDbEndpoint",
                service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
                subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)],
            )
            table_arn = table.table_arn
            # Same split as the task role: item operations on the table, Query on the index only.
            # (The run store's one write transaction contains only Put items, which IAM authorises
            # as dynamodb:PutItem; there is no separate TransactWriteItems IAM action.)
            dynamodb_endpoint.add_to_policy(
                iam.PolicyStatement(
                    sid="RunHistoryItemOperations",
                    principals=[iam.AnyPrincipal()],  # required in endpoint policies
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                    ],
                    resources=[table_arn],
                )
            )
            dynamodb_endpoint.add_to_policy(
                iam.PolicyStatement(
                    sid="RunHistoryListIndexQuery",
                    principals=[iam.AnyPrincipal()],
                    actions=["dynamodb:Query"],
                    resources=[f"{table_arn}/index/{schema.LIST_INDEX}"],
                )
            )
            api_service = ApiService(
                self,
                "ApiService",
                vpc=vpc,
                listener=https,
                repository=repository,
                table=table,
                config=service,
                api_hostname=config.api_hostname,
            )
            CfnOutput(self, "ClusterName", value=api_service.cluster.cluster_name)
            CfnOutput(self, "ServiceName", value=api_service.service.service_name)

        CfnOutput(self, "ApiHostname", value=config.api_hostname)
        CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        CfnOutput(self, "AlbCanonicalHostedZoneId", value=alb.load_balancer_canonical_hosted_zone_id)


class EnergyFrontendStack(Stack):
    """Private S3 bucket behind CloudFront for the energy frontend hostname."""

    def __init__(self, scope: Construct, construct_id: str, *, config: EdgeConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", PROJECT_TAG)
        Tags.of(self).add("Component", "frontend-edge")

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=False, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
            ),
        )
        # The viewer certificate must be a us-east-1 ACM certificate for this hostname only.
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            domain_names=[config.frontend_hostname],
            certificate=acm.Certificate.from_certificate_arn(
                self, "FrontendCertificate", config.frontend_certificate_arn
            ),
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
            ),
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=status, response_http_status=200, response_page_path="/index.html"
                )
                for status in (403, 404)
            ],
        )

        CfnOutput(self, "FrontendHostname", value=config.frontend_hostname)
        CfnOutput(self, "DistributionDomainName", value=distribution.distribution_domain_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "SiteBucketName", value=bucket.bucket_name)
