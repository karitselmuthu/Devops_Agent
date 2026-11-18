from agents_platform.app_deployments import AppDeployment, AppPlanRequest, DeploymentStatus
from agents_platform.app_runtime import AppJobQueue, app_job_once, dispatch_app_jobs_once
from agents_platform.app_store import InMemoryAppStore


def test_app_job_dispatch_and_worker_lifecycle() -> None:
    store = InMemoryAppStore()
    plan = AppDeployment(
        request=AppPlanRequest(repository_url="https://github.com/acme/web-app", ref="main",
                               name="web-app", port=8080),
        source_commit="a" * 40, created_by="admin", resources=[],
        ecr_repository="agents-app-staging-test", stack_name="AgentsApp-staging-test",
        image_tag="a" * 12,
    )
    store.create(plan)
    store.approve(plan.id)

    class Client:
        messages = []

        def send_message(self, **kwargs):
            self.messages.append(kwargs["MessageBody"])

        def receive_message(self, **kwargs):
            if not self.messages:
                return {}
            return {"Messages": [{"Body": self.messages.pop(0), "ReceiptHandle": "receipt"}]}

        def delete_message(self, **kwargs):
            pass

    class Deployer:
        def deploy(self, current, target_store):
            target_store.set_status(current.id, DeploymentStatus.RUNNING,
                                    service_url="https://app.example.com")

        def delete(self, current, target_store):
            target_store.set_status(current.id, DeploymentStatus.DELETED, service_url=None)

    queue = AppJobQueue("queue-url", Client())
    assert dispatch_app_jobs_once(store, queue) == 1
    assert dispatch_app_jobs_once(store, queue) == 0
    assert app_job_once(store, queue, Deployer())
    assert store.get(plan.id).status == DeploymentStatus.RUNNING
    store.request_delete(plan.id)
    assert dispatch_app_jobs_once(store, queue) == 1
    assert app_job_once(store, queue, Deployer())
    assert store.get(plan.id).status == DeploymentStatus.DELETED
