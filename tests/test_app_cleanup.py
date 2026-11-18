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


def test_bootstrap_asset_check_is_read_only_and_scoped() -> None:
    class CloudFormation:
        def get_paginator(self, action):
            return SimpleNamespace(paginate=lambda: [{"StackSummaries": []}])

    class S3:
        def list_buckets(self):
            return {"Buckets": [{"Name": "cdk-hnb659fds-assets-123456789012-us-east-1"},
                                {"Name": "other-bucket"}]}

        def delete_bucket(self, **kwargs):
            pytest.fail("Read-only check tried to delete S3 bucket")

    class Ecr:
        exceptions = SimpleNamespace(RepositoryNotFoundException=LookupError)

        def describe_repositories(self, **kwargs):
            raise LookupError

        def delete_repository(self, **kwargs):
            pytest.fail("Read-only check tried to delete ECR repository")

    clients = {"cloudformation": CloudFormation(), "s3": S3(), "ecr": Ecr()}
    session = SimpleNamespace(client=lambda name: clients[name])
    aws_cleanup.bootstrap_asset_leftovers(session, "123456789012", "us-east-1", delete=False)


def test_bootstrap_asset_cleanup_refuses_live_stacks() -> None:
    class CloudFormation:
        def get_paginator(self, action):
            return SimpleNamespace(paginate=lambda: [{"StackSummaries": [
                {"StackName": "AgentsPlatform-staging", "StackStatus": "CREATE_COMPLETE"}
            ]}])

    session = SimpleNamespace(client=lambda name: CloudFormation() if name == "cloudformation"
                              else pytest.fail("Inspected assets while stack is active"))
    with pytest.raises(ValueError, match="Active stacks"):
        aws_cleanup.bootstrap_asset_leftovers(session, "123456789012", "us-east-1", delete=True)


def test_bootstrap_asset_cleanup_deletes_only_confirmed_assets(monkeypatch) -> None:
    calls = []
    bucket = "cdk-hnb659fds-assets-123456789012-us-east-1"
    repository = "cdk-hnb659fds-container-assets-123456789012-us-east-1"

    class CloudFormation:
        def get_paginator(self, action):
            return SimpleNamespace(paginate=lambda: [{"StackSummaries": []}])

    class S3:
        def list_buckets(self):
            return {"Buckets": [{"Name": bucket}, {"Name": "unrelated"}]}

        def delete_bucket(self, **kwargs):
            calls.append(("bucket", kwargs["Bucket"]))

    class Ecr:
        exceptions = SimpleNamespace(RepositoryNotFoundException=LookupError)

        def describe_repositories(self, **kwargs):
            assert kwargs["repositoryNames"] == [repository]
            return {"repositories": [{"repositoryName": repository}]}

        def delete_repository(self, **kwargs):
            calls.append(("repository", kwargs["repositoryName"]))

    clients = {"cloudformation": CloudFormation(), "s3": S3(), "ecr": Ecr()}
    session = SimpleNamespace(client=lambda name: clients[name])
    monkeypatch.setattr(aws_cleanup, "empty_bucket", lambda client, name: calls.append(("empty", name)))
    monkeypatch.setattr("builtins.input", lambda prompt: "DELETE BOOTSTRAP ASSETS 123456789012 us-east-1")
    aws_cleanup.bootstrap_asset_leftovers(session, "123456789012", "us-east-1", delete=True)
    assert calls == [("empty", bucket), ("bucket", bucket), ("repository", repository)]
