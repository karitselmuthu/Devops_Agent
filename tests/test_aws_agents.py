import json
from datetime import datetime, timezone

import pytest

from agents.infrastructure_agent import InfrastructureAgent
from agents.monitoring_agent import MonitoringAgent
from agents_shared.models import (
    AgentKind,
    CloudFormationTarget,
    CloudWatchMetricTarget,
    WorkflowRequest,
    WorkflowTask,
)
from planning_agent.planner import create_plan


class FakeCloudFormation:
    def __init__(self, replacement: str = "False", paged: bool = False) -> None:
        self.replacement = replacement
        self.paged = paged
        self.executed = False
        self.start_args: dict | None = None

    def describe_stacks(self, **kwargs: str) -> dict:
        return {"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}

    def create_change_set(self, **kwargs: str) -> dict:
        self.start_args = kwargs
        return {"Id": "change-set-123"}

    def describe_change_set(self, **kwargs: str) -> dict:
        if self.paged and "NextToken" not in kwargs:
            return {
                "Status": "CREATE_COMPLETE",
                "Changes": [{"ResourceChange": {"Action": "Modify", "Replacement": "False"}}],
                "NextToken": "page-2",
            }
        return {
            "Status": "CREATE_COMPLETE",
            "Changes": [{"ResourceChange": {"Action": "Modify", "Replacement": self.replacement}}],
        }

    def execute_change_set(self, **kwargs: str) -> dict:
        self.executed = True
        return {}


def test_infrastructure_inspect_and_guarded_update() -> None:
    client = FakeCloudFormation()
    agent = InfrastructureAgent({"us-east-1": client}, poll_interval=0.01)
    inspect_target = CloudFormationTarget(stack_name="api", region="us-east-1")
    inspect_task = WorkflowTask(agent=AgentKind.INFRA, instruction="inspect", infrastructure_target=inspect_target)
    assert json.loads(agent.execute(inspect_task))["status"] == "UPDATE_COMPLETE"

    deploy_target = CloudFormationTarget(
        stack_name="api", region="us-east-1", action="deploy",
        template_url="https://example.com/template.yaml",
    )
    deploy_task = WorkflowTask(agent=AgentKind.INFRA, instruction="update", infrastructure_target=deploy_target)
    assert json.loads(agent.execute(deploy_task))["status"] == "UPDATE_COMPLETE"
    assert client.start_args["ChangeSetType"] == "UPDATE"
    assert client.executed


def test_infrastructure_replacement_requires_review() -> None:
    client = FakeCloudFormation(replacement="True", paged=True)
    agent = InfrastructureAgent({"us-east-1": client})
    target = CloudFormationTarget(
        stack_name="api", region="us-east-1", action="deploy",
        template_url="https://example.com/template.yaml",
    )
    with pytest.raises(RuntimeError, match="manual review"):
        agent.execute(WorkflowTask(agent=AgentKind.INFRA, instruction="update", infrastructure_target=target))
    assert not client.executed


class FakeCloudWatch:
    def __init__(self, datapoints: list[dict]) -> None:
        self.datapoints = datapoints
        self.request: dict | None = None

    def get_metric_statistics(self, **kwargs: object) -> dict:
        self.request = kwargs
        return {"Datapoints": self.datapoints}


def test_monitoring_evaluates_window_and_missing_data() -> None:
    target = CloudWatchMetricTarget(
        region="us-east-1", namespace="Application", metric_name="Availability",
        dimensions={"Service": "api"}, statistic="Average", comparison="gte", threshold=99,
    )
    task = WorkflowTask(agent=AgentKind.MONITORING, instruction="check", monitoring_target=target)
    client = FakeCloudWatch([
        {"Timestamp": datetime.now(timezone.utc), "Average": 100, "SampleCount": 9},
        {"Timestamp": datetime.now(timezone.utc), "Average": 90, "SampleCount": 1},
    ])
    agent = MonitoringAgent({"us-east-1": client})
    assert json.loads(agent.execute(task))["observed"] == 99
    assert client.request["Dimensions"] == [{"Name": "Service", "Value": "api"}]
    client.datapoints = []
    with pytest.raises(RuntimeError, match="no metric data"):
        agent.execute(task)


def test_plan_carries_aws_targets() -> None:
    infra = CloudFormationTarget(stack_name="api", region="us-east-1")
    monitoring = CloudWatchMetricTarget(
        region="us-east-1", namespace="Application", metric_name="Availability",
        comparison="gte", threshold=99,
    )
    plan = create_plan(
        WorkflowRequest(
            request="deploy", service="api", environment="prod",
            infrastructure_target=infra, monitoring_target=monitoring,
        )
    )
    assert plan.tasks[0].infrastructure_target == infra
    assert plan.tasks[1].monitoring_target == monitoring
