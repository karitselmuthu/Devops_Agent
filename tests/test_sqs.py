from uuid import uuid4

from agents_shared.models import AgentKind, TaskResultMessage, TaskStatus, WorkflowTask, WorkflowTaskMessage
from agents_shared.sqs import SqsTaskBroker


class FakeSqs:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.deleted: list[dict] = []
        self.body: str | None = None

    def send_message(self, **kwargs) -> None:
        self.sent.append(kwargs)

    def receive_message(self, **kwargs) -> dict:
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "receipt"}]} if self.body else {}

    def delete_message(self, **kwargs) -> None:
        self.deleted.append(kwargs)

    def change_message_visibility(self, **kwargs) -> None:
        pass


def test_sqs_serializes_and_acknowledges_deliveries() -> None:
    client = FakeSqs()
    queues = {agent: f"https://sqs.example/{agent}.fifo" for agent in AgentKind}
    broker = SqsTaskBroker(queues, "https://sqs.example/results.fifo", client)
    message = WorkflowTaskMessage(workflow_id=uuid4(), task=WorkflowTask(agent=AgentKind.CODE, instruction="do"))
    broker.publish(message)
    assert client.sent[0]["MessageDeduplicationId"] == str(message.task.id)
    client.body = client.sent[0]["MessageBody"]
    delivery = broker.receive_task_delivery(AgentKind.CODE)
    assert delivery is not None
    assert delivery.message == message
    delivery.ack()
    assert client.deleted[0]["ReceiptHandle"] == "receipt"

    result = TaskResultMessage(
        workflow_id=message.workflow_id, task_id=message.task.id,
        status=TaskStatus.COMPLETED, output="done",
    )
    broker.publish_result(result)
    assert client.sent[1]["MessageGroupId"] == str(message.workflow_id)
