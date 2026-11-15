"""SQS transport for separate API, agent, and result-consumer processes."""

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from agents_shared.models import AgentKind, TaskResultMessage, WorkflowTaskMessage


MessageT = TypeVar("MessageT")


@dataclass
class Delivery(Generic[MessageT]):
    message: MessageT
    ack: Callable[[], None]
    extend_visibility: Callable[[], None]


class SqsTaskBroker:
    def __init__(
        self,
        queue_urls: dict[AgentKind, str],
        result_queue_url: str,
        client: object | None = None,
    ) -> None:
        if set(queue_urls) != set(AgentKind):
            raise ValueError("A queue URL is required for every agent")
        if not result_queue_url:
            raise ValueError("A result queue URL is required")
        if client is None:
            import boto3

            client = boto3.client("sqs")
        self.client = client
        self.queue_urls = queue_urls
        self.result_queue_url = result_queue_url

    def _send(self, url: str, body: str, group: str, deduplication_id: str) -> None:
        args = {"QueueUrl": url, "MessageBody": body}
        if url.endswith(".fifo"):
            args.update(MessageGroupId=group, MessageDeduplicationId=deduplication_id)
        self.client.send_message(**args)

    def publish(self, message: WorkflowTaskMessage) -> None:
        self._send(
            self.queue_urls[message.task.agent],
            message.model_dump_json(),
            str(message.workflow_id),
            str(message.task.id),
        )

    def publish_result(self, result: TaskResultMessage) -> None:
        self._send(
            self.result_queue_url,
            result.model_dump_json(),
            str(result.workflow_id),
            f"{result.task_id}:{result.status}",
        )

    def _receive(self, url: str, model: type[MessageT]) -> Delivery[MessageT] | None:
        response = self.client.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=120,
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        raw = messages[0]
        message = model.model_validate_json(raw["Body"])
        receipt = raw["ReceiptHandle"]
        return Delivery(
            message=message,
            ack=lambda: self.client.delete_message(QueueUrl=url, ReceiptHandle=receipt),
            extend_visibility=lambda: self.client.change_message_visibility(
                QueueUrl=url, ReceiptHandle=receipt, VisibilityTimeout=120
            ),
        )

    def receive_task_delivery(self, agent: AgentKind) -> Delivery[WorkflowTaskMessage] | None:
        return self._receive(self.queue_urls[agent], WorkflowTaskMessage)

    def receive_result_delivery(self) -> Delivery[TaskResultMessage] | None:
        return self._receive(self.result_queue_url, TaskResultMessage)
