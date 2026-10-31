"""Step Functions Standard workflow that chains the four pipeline stage Lambdas (Phase 9.5).

The definition comes from the Phase 6 generator (infrastructure/run_control/spec.py). The stage
functions are imported by their stable exports from IrishEnergyPipelineStack, so none is created
here. Defining the state machine starts nothing: executions come only from StartExecution, which
no deployed component calls while pipeline execution is disabled.
"""

import json

from aws_cdk import CfnOutput, Fn, Stack, Tags
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct
from energy_config import (
    ACCOUNT,
    PIPELINE_FUNCTION_EXPORT,
    PIPELINE_STAGES,
    RUN_HISTORY_TABLE_NAME,
    STATE_MACHINE_NAME,
)

from infrastructure.run_control import spec


def stage_label(key: str) -> str:
    return "".join(part.capitalize() for part in key.split("_"))


class EnergyWorkflowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.ITable,
        pipeline_stack: Stack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("Project", "irish-energy-data-platform")
        Tags.of(self).add("Component", "workflow")
        self.add_dependency(pipeline_stack)

        function_arns = {
            stage.key: Fn.import_value(PIPELINE_FUNCTION_EXPORT.format(label=stage_label(stage.key)))
            for stage in PIPELINE_STAGES
        }
        definition = spec.build_state_machine_definition(
            table=RUN_HISTORY_TABLE_NAME, stage_function_arns=function_arns
        )

        # Separate from the four Lambda roles; only what the definition's states call.
        role = iam.Role(
            self,
            "StateMachineRole",
            assumed_by=iam.ServicePrincipal(
                "states.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": ACCOUNT}}
            ),
            description="Pipeline state machine: invoke the four stages, record run progress",
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RecordProgress",
                actions=["dynamodb:UpdateItem", "dynamodb:DeleteItem"],
                resources=[table.table_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeStageFunctions",
                actions=["lambda:InvokeFunction"],
                resources=[function_arns[stage.key] for stage in PIPELINE_STAGES],
            )
        )

        self.state_machine = sfn.StateMachine(
            self,
            "PipelineStateMachine",
            state_machine_name=STATE_MACHINE_NAME,
            state_machine_type=sfn.StateMachineType.STANDARD,
            # json.dumps, not Stack.to_json_string: jsii drops None, and a missing
            # "ResultPath": null would make a DynamoDB task replace the whole state.
            definition_body=sfn.DefinitionBody.from_string(json.dumps(definition, separators=(",", ":"))),
            role=role,
        )
        CfnOutput(
            self,
            "StateMachineArn",
            value=self.state_machine.state_machine_arn,
            export_name="IrishEnergyPipelineWorkflow-StateMachineArn",
        )
