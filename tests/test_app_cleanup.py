from types import SimpleNamespace

import pytest

from scripts import aws_cleanup


def test_managed_app_cleanup_only_deletes_owned_stacks() -> None:
    calls = []
    name = "AgentsApp-staging-123456789abc"

    class CloudFormation:
        def get_paginator(self, action):
            assert action == "list_stacks"
            return SimpleNamespace(paginate=lambda: [{"StackSummaries": [
                {"StackName": name, "StackStatus": "CREATE_COMPLETE"},
                {"StackName": "OtherStack", "StackStatus": "CREATE_COMPLETE"},
            ]}])

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"Tags": [{"Key": "ManagedBy", "Value": "AgentsPlatform"}]}]}

        def delete_stack(self, **kwargs):
            calls.append(("stack", kwargs["StackName"]))

        def get_waiter(self, name):
            return SimpleNamespace(wait=lambda **kwargs: calls.append(("wait", kwargs["StackName"])))

    class Ecr:
        exceptions = SimpleNamespace(RepositoryNotFoundException=RuntimeError)

        def get_paginator(self, action):
            assert action == "describe_repositories"
            return SimpleNamespace(paginate=lambda: [{"repositories": [{
                "repositoryName": "agents-app-staging-123456789abc",
                "repositoryArn": "arn:aws:ecr:us-east-1:123456789012:repository/agents-app-staging-123456789abc",
            }]}])

        def list_tags_for_resource(self, **kwargs):
            return {"tags": [{"Key": "ManagedBy", "Value": "AgentsPlatform"}]}

        def delete_repository(self, **kwargs):
            calls.append(("ecr", kwargs["repositoryName"]))

    aws_cleanup.delete_managed_apps(CloudFormation(), Ecr(), "staging")
    assert calls == [("stack", name), ("wait", name),
                     ("ecr", "agents-app-staging-123456789abc")]


def test_cleanup_rejects_unowned_app_stack() -> None:
    class CloudFormation:
        def get_paginator(self, action):
            return SimpleNamespace(paginate=lambda: [{"StackSummaries": [
                {"StackName": "AgentsApp-staging-123456789abc", "StackStatus": "CREATE_COMPLETE"}
            ]}])

        def describe_stacks(self, **kwargs):
            return {"Stacks": [{"Tags": []}]}

    with pytest.raises(ValueError, match="ownership"):
        aws_cleanup.managed_app_stacks(CloudFormation(), "staging")
