"""Cognito authentication resources for the Irish Energy Analytics platform."""

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_cognito as cognito
from constructs import Construct


class EnergyCognitoStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "authentication")

        self.user_pool = cognito.UserPool(
            self,
            "EnergyUserPool",
            user_pool_name="irish-energy-analytics",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.operator_group = cognito.CfnUserPoolGroup(
            self,
            "OperatorGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="operator",
            description="Single operator account for the portfolio platform",
        )

        self.app_client = self.user_pool.add_client(
            "EnergyWebAppClient",
            user_pool_client_name="irish-energy-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_srp=True,
                user_password=False,
                admin_user_password=False,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID],
                callback_urls=[
                    "https://energy.ai-autofy-general-server.com/"
                ],
                logout_urls=[
                    "https://energy.ai-autofy-general-server.com/"
                ],
            ),
            prevent_user_existence_errors=True,
        )

        self.user_pool_domain = self.user_pool.add_domain(
            "EnergyHostedUiDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix="irish-energy-analytics-851725545966"
            ),
        )

        CfnOutput(
            self,
            "UserPoolId",
            value=self.user_pool.user_pool_id,
        )
        CfnOutput(
            self,
            "AppClientId",
            value=self.app_client.user_pool_client_id,
        )
        CfnOutput(
            self,
            "HostedUiDomain",
            value=f"https://{self.user_pool_domain.domain_name}",
        )
