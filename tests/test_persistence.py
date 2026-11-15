from agents_platform.persistence import Base, DurableWorkflowStore
from agents_shared.models import AgentKind, GitHubSourceTarget, TaskResultMessage, TaskStatus, WorkflowRequest


def test_store_survives_reopen_and_queues_each_step_once(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'workflows.db'}"
    store = DurableWorkflowStore(url)
    Base.metadata.create_all(store.engine)
    plan = store.create(WorkflowRequest(
        request="scan then release", service="api", environment="test",
        source_target=GitHubSourceTarget(owner="acme", repo="api", ref="main"),
        devops_target={"provider": "aws_codepipeline", "name": "api", "region": "us-east-1"},
    ))
    first = store.pending_outbox()
    assert len(first) == 1
    assert first[0].task.agent == AgentKind.SECURITY
    store.mark_sent(first[0].task.id)
    assert store.pending_outbox() == []

    reopened = DurableWorkflowStore(url)
    assert reopened.get(plan.id).tasks[0].status == TaskStatus.QUEUED
    result = TaskResultMessage(
        workflow_id=plan.id, task_id=first[0].task.id,
        status=TaskStatus.COMPLETED, output="done",
    )
    updated = reopened.apply_result(result)
    assert updated.tasks[1].status == TaskStatus.QUEUED
    assert reopened.apply_result(result) == updated
    assert len(reopened.pending_outbox()) == 1
    assert reopened.claim_task(first[0].task.id)
    assert not reopened.claim_task(first[0].task.id)
    reopened.save_claimed_result(result)
    assert reopened.claimed_result(first[0].task.id) == result
