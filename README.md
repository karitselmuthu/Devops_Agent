# DevOps Agents Platform

A Python platform for coordinating code proposals, security scans, CI/CD releases,
AWS infrastructure changes, and post-release monitoring. FastAPI exposes the API;
PostgreSQL stores workflows; Amazon SQS carries tasks between independent workers.
Local mode uses an in-memory store and broker for development.

## Project status

The application and AWS deployment templates are implemented and tested locally.
An AWS environment has not yet been deployed. The Code Agent produces reviewable
patches; it does not publish changes. The integration hub exposes HTTP tools and
does not implement the MCP wire protocol.

## Architecture

```text
Client -> FastAPI -> PostgreSQL workflow + outbox -> SQS agent queues
                                                  -> Python workers
                                                  -> SQS results -> orchestrator
```

| Component | Role |
| --- | --- |
| API and orchestrator | Validate requests, authorize users, schedule workflow stages |
| Planning agent | Build a deterministic plan from typed targets |
| Code agent | Generate and validate a patch proposal |
| Security agent | Run Bandit, Trivy, and Gitleaks scans |
| DevOps agent | Dispatch and monitor GitHub Actions or AWS CodePipeline |
| Infrastructure agent | Inspect or update CloudFormation stacks |
| Monitoring agent | Evaluate CloudWatch metrics against thresholds |
| Integration hub | Expose read-only status and metric tools over HTTP |

## Workflow behavior

`POST /workflows` accepts a target for each requested stage. A deployment can run
source scanning, CI/CD, CloudFormation inspection or update, and CloudWatch metric
evaluation in that order. Only stages with targets are queued. A failed stage stops
the sequence. `code_change: true` creates a separate Code Agent proposal workflow;
its patch is returned for review and is **not** committed, scanned, or deployed.

```json
{
  "request": "Release the billing service",
  "service": "billing",
  "environment": "staging",
  "source_target": {"provider": "github", "owner": "acme", "repo": "billing", "ref": "main"},
  "devops_target": {"provider": "github_actions", "owner": "acme", "repo": "billing", "workflow_id": "deploy.yml", "ref": "main", "inputs": {"environment": "staging"}},
  "infrastructure_target": {"provider": "aws_cloudformation", "stack_name": "billing", "region": "us-east-1", "action": "inspect"},
  "monitoring_target": {"provider": "aws_cloudwatch", "region": "us-east-1", "namespace": "Billing", "metric_name": "Availability", "comparison": "gte", "threshold": 99}
}
```

For CodePipeline, set `devops_target` to
`{"provider":"aws_codepipeline","name":"billing-deploy","region":"us-east-1"}`.
For a code proposal, pass `code_change: true` and `source_target`, without a
deployment target. The proposal uses the configured OpenAI model and validates the
diff with `git apply --check`.

## Local development

```bash
uv sync --locked --extra test --extra code-agent --extra security-agent
uv run python main.py
uv run --no-sync pytest -q
```

Browse `http://127.0.0.1:8000/docs`. The API runs in local mode unless
`AGENTS_MODE=aws`; local workflows need the in-process worker loop to advance.
Local mode has no authentication and must stay on a developer machine.

## AWS layout

The Python CDK stack in `infra/app.py` provisions a VPC, private ECS Fargate
services for the API, dispatcher, result consumer, and five agents, an HTTPS
Application Load Balancer, RDS PostgreSQL, encrypted FIFO SQS queues with a
dead-letter queue, ECR, Secrets Manager, CloudWatch Logs, and a Cognito user
pool. `migrations/` contains the Alembic schema. The task outbox commits with
workflow state; the dispatcher publishes pending tasks to SQS. Task claims and
saved results protect against duplicate deliveries. Workers obtain AWS
credentials from their task roles.

The integration hub exposes four authenticated, read-only HTTP tools under
`/integrations/tools`. It is an MCP-style connector hub, not a protocol MCP
server. `agents_shared` contains the Pydantic contracts and broker adapter.

## AWS release setup

1. Choose an AWS account, Region, public Route 53 hosted zone, API domain, and
   an ACM certificate for that domain in the same Region as the load balancer.
   Bootstrap the account and Region with `npx aws-cdk bootstrap` using an
   authorized deployment identity.
2. Create `staging` and, if needed,
   `production` GitHub environments. Protect production with environment
   reviewers and restrict deployment branches.
3. Configure each environment's variables: `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`,
   `CERTIFICATE_ARN`, `API_DOMAIN`, `HOSTED_ZONE_ID`, `OPENAI_MODEL`,
   `ALLOWED_REPOSITORIES`, `ALLOWED_PIPELINES`, and `ALLOWED_STACKS`. The three
   allowlists are comma-separated. Set environment secrets
   `AGENTS_GITHUB_TOKEN` and `AGENTS_OPENAI_API_KEY`.
4. Create a GitHub OIDC-trusting AWS deployment role scoped to the repository's
   environment subject. It needs CloudFormation/CDK bootstrap deployment,
   ECR push, ECS task run and describe, Secrets Manager write for the two app
   secrets, and `iam:PassRole` for the migration task. Set its ARN as
   `AWS_DEPLOY_ROLE_ARN`.
5. If agents manage CodePipeline or CloudFormation stacks, set CDK context
   `codepipeline_arns` and `managed_stack_arns` in `cdk.json` to the specific
   resources and grant the worker roles any additional permissions needed by
   their change sets. Review those permissions before enabling deploy targets.
6. Push `main` to run tests, then run the manual `Deploy AWS` workflow for
   staging. The release creates the stack
   with services at zero, builds and pushes the image, runs the migration task,
   starts services, and checks `GET /health`. Use the manual `Deploy AWS`
   workflow for production. Confirm ECS service stability, SQS dead-letter
   counts, and an authenticated workflow before accepting the release.

The deployment script is `scripts/deploy_aws.py`. It restores the previous image
and desired count when the post-deploy health check fails. Database migrations
must be backward compatible because an image rollback does not reverse schema
changes.

The Cognito pool creates `agents-readers`, `agents-operators`, and
`agents-admins` groups. Create users and assign groups before invoking the API.
Readers can inspect workflows and tools, operators can create workflows, and
administrators can request CloudFormation deploy actions. The API verifies
Cognito access tokens; `/health` is public for the load balancer.

## Operational limits

- A claimed task interrupted before saving its result remains claimed. Inspect
  its external side effect, then reconcile the task manually; do not blindly
  redispatch GitHub Actions or CloudFormation operations. Watch the SQS
  dead-letter queue and task claims without results.
- The Code Agent proposes a patch only. Publishing changes and linking them to
  a scanned commit require a separate review and release process.
- The deterministic planner follows typed targets. It does not infer targets or
  safely authorize arbitrary LLM-generated actions.
- `GET /health` checks the API process only. Production readiness also requires
  database, queue, worker, and external connector checks.
