from agents_shared.models import AgentKind, WorkflowPlan, WorkflowRequest, WorkflowTask


def create_plan(request: WorkflowRequest) -> WorkflowPlan:
    """Create only stages with enough information to execute."""
    if request.code_change:
        steps = [(AgentKind.CODE, "Propose application changes")]
    else:
        steps = []
        if request.source_target:
            steps.append((AgentKind.SECURITY, "Scan application source"))
        if request.devops_target:
            steps.append((AgentKind.DEVOPS, "Run the release pipeline"))
        if request.infrastructure_target:
            steps.append((AgentKind.INFRA, "Inspect or update infrastructure"))
        if request.monitoring_target:
            steps.append((AgentKind.MONITORING, "Evaluate the service metric"))
    tasks = [
        WorkflowTask(
            agent=agent,
            instruction=f"{action} for {request.service} in {request.environment}: {request.request}",
            source_target=request.source_target if agent in {AgentKind.CODE, AgentKind.SECURITY} else None,
            devops_target=request.devops_target if agent == AgentKind.DEVOPS else None,
            infrastructure_target=request.infrastructure_target if agent == AgentKind.INFRA else None,
            monitoring_target=request.monitoring_target if agent == AgentKind.MONITORING else None,
        )
        for agent, action in steps
    ]
    return WorkflowPlan(request=request, tasks=tasks)
