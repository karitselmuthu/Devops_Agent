"""AWS CloudFormation stack inspection and guarded updates."""

import json
import time

from agents_shared.models import AgentKind, CloudFormationTarget, WorkflowTask


class InfrastructureAgent:
    def __init__(
        self,
        clients: dict[str, object] | None = None,
        *,
        poll_interval: float = 10,
        timeout_seconds: float = 900,
    ) -> None:
        if poll_interval <= 0 or timeout_seconds <= 0:
            raise ValueError("poll_interval and timeout_seconds must be positive")
        self.clients = clients if clients is not None else {}
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds

    def _client(self, region: str) -> object:
        if region not in self.clients:
            import boto3

            self.clients[region] = boto3.Session(region_name=region).client("cloudformation")
        return self.clients[region]

    def _wait_change_set(self, client: object, stack_name: str, change_set_id: str) -> dict:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            change_set = client.describe_change_set(
                StackName=stack_name, ChangeSetName=change_set_id
            )
            if change_set["Status"] == "CREATE_COMPLETE":
                return change_set
            if change_set["Status"] == "FAILED":
                reason = change_set.get("StatusReason", "Change set creation failed")
                if "didn't contain changes" in reason or "No updates are to be performed" in reason:
                    return {"Status": "NO_CHANGES", "Changes": []}
                raise RuntimeError("CloudFormation change set failed")
            if time.monotonic() >= deadline:
                raise TimeoutError("CloudFormation change set status is unknown")
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))

    def _wait_stack(self, client: object, stack_name: str) -> str:
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            status = client.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
            if status == "UPDATE_COMPLETE":
                return status
            if status.endswith("FAILED") or "ROLLBACK" in status or status == "DELETE_COMPLETE":
                raise RuntimeError(f"CloudFormation stack update failed: {status}")
            if time.monotonic() >= deadline:
                raise TimeoutError("CloudFormation stack status is unknown")
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))

    def execute(self, task: WorkflowTask) -> str:
        if task.agent != AgentKind.INFRA or task.infrastructure_target is None:
            raise ValueError("Infrastructure task requires a CloudFormation target")
        target: CloudFormationTarget = task.infrastructure_target
        client = self._client(target.region)
        if target.action == "inspect":
            stack = client.describe_stacks(StackName=target.stack_name)["Stacks"][0]
            status = stack["StackStatus"]
            if status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"}:
                raise RuntimeError(f"CloudFormation stack is not healthy: {status}")
            return json.dumps({"stack": target.stack_name, "status": status})

        response = client.create_change_set(
            StackName=target.stack_name,
            ChangeSetName=f"agents-{task.id}",
            ChangeSetType="UPDATE",
            TemplateURL=target.template_url,
            Capabilities=target.capabilities,
        )
        change_set_id = response["Id"]
        change_set = self._wait_change_set(client, target.stack_name, change_set_id)
        if change_set["Status"] == "NO_CHANGES":
            return json.dumps({"stack": target.stack_name, "status": "NO_CHANGES"})
        while True:
            for change in change_set.get("Changes", []):
                resource = change.get("ResourceChange", {})
                if resource.get("Action") == "Remove" or resource.get("Replacement") in {"True", "Conditional"}:
                    raise RuntimeError(f"Change set {change_set_id} needs manual review")
            next_token = change_set.get("NextToken")
            if not next_token:
                break
            change_set = client.describe_change_set(
                StackName=target.stack_name, ChangeSetName=change_set_id, NextToken=next_token
            )
        client.execute_change_set(
            StackName=target.stack_name,
            ChangeSetName=change_set_id,
            ClientRequestToken=str(task.id),
        )
        status = self._wait_stack(client, target.stack_name)
        return json.dumps({"stack": target.stack_name, "change_set_id": change_set_id, "status": status})
