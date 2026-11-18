"""SQS delivery and worker loop for approved application jobs."""

import json
import logging
import os
import signal
from threading import Event, Thread

import boto3

from agents_platform.app_aws import AppAwsDeployer
from agents_platform.app_deployments import DeploymentStatus
from agents_platform.app_store import AppJob, DurableAppStore
from agents_platform.config import database_url


logger = logging.getLogger(__name__)


class AppJobQueue:
    def __init__(self, queue_url: str, client=None) -> None:
        self.queue_url = queue_url
        self.client = client or boto3.client("sqs")

    def publish(self, job: AppJob) -> None:
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=job.model_dump_json(),
            MessageGroupId=str(job.deployment_id),
            MessageDeduplicationId=str(job.id),
        )

    def receive(self) -> tuple[AppJob, str] | None:
        response = self.client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=10,
            VisibilityTimeout=120,
        )
        messages = response.get("Messages", [])
        if not messages:
            return None
        message = messages[0]
        return AppJob.model_validate(json.loads(message["Body"])), message["ReceiptHandle"]

    def extend(self, receipt: str) -> None:
        self.client.change_message_visibility(
            QueueUrl=self.queue_url, ReceiptHandle=receipt, VisibilityTimeout=120,
        )

    def ack(self, receipt: str) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)


def dispatch_app_jobs_once(store: DurableAppStore, queue: AppJobQueue) -> int:
    jobs = store.pending_jobs()
    for job in jobs:
        queue.publish(job)
        store.mark_sent(job.id)
    return len(jobs)


def app_job_once(store: DurableAppStore, queue: AppJobQueue, deployer: AppAwsDeployer) -> bool:
    delivery = queue.receive()
    if delivery is None:
        return False
    job, receipt = delivery
    stop_heartbeat = Event()

    def heartbeat() -> None:
        while not stop_heartbeat.wait(60):
            queue.extend(receipt)

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        plan = store.get(job.deployment_id)
        try:
            if job.action == "deploy" and plan.status in {
                DeploymentStatus.APPROVED, DeploymentStatus.BUILDING, DeploymentStatus.DEPLOYING,
            }:
                deployer.deploy(plan, store)
            elif job.action == "delete" and plan.status == DeploymentStatus.DELETING:
                deployer.delete(plan, store)
        except Exception as exc:
            logger.exception("Application job failed", extra={"deployment_id": str(job.deployment_id)})
            store.set_status(job.deployment_id, DeploymentStatus.FAILED, error=str(exc)[:1000])
        queue.ack(receipt)
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=5)
    return True


def main() -> int:
    store = DurableAppStore(database_url())
    queue = AppJobQueue(os.environ["SQS_APP_DEPLOY_URL"])
    deployer = AppAwsDeployer.from_environment()
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        app_job_once(store, queue, deployer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
