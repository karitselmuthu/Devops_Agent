# DevOps Agents Platform

A Python platform for coordinating code proposals, security scans, CI/CD releases,
AWS infrastructure changes, and post-release monitoring. FastAPI exposes the API;
PostgreSQL stores workflows; Amazon SQS carries tasks between independent workers.
Local mode uses an in-memory store and broker for development.

## Project status

The application, AWS deployment templates, and a first web portal for source checks
are implemented and tested locally.
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

Run these commands from the repository directory on the **same Mac** where you
will open the browser. AWS credentials, Docker, PostgreSQL, and SQS are not needed
to view the local portal.

1. Open **Terminal 1** and install the locked Python dependencies:

   ```bash
   cd /Users/karthikeyan/PycharmProjects/DevOps_Agent
   uv sync --locked --extra test --extra code-agent --extra security-agent
   ```

2. Start the server in **Terminal 1** and leave that terminal open:

   ```bash
   unset AGENTS_MODE
   uv run --no-sync python main.py
   ```

   Wait for `Uvicorn running on http://127.0.0.1:8000`. If an older server is
   running in another terminal, stop it with `Ctrl+C` before starting this one.
   Closing Terminal 1 or pressing `Ctrl+C` stops the site.

3. Open **Terminal 2** and confirm the server responds:

   ```bash
   curl http://127.0.0.1:8000/health
   ```

   Expected output: `{"status":"ok"}`. Then open the **web portal** at
   <http://127.0.0.1:8000/>. Swagger remains available at
   <http://127.0.0.1:8000/docs>.

4. Install the two system scanners before running a source check on macOS:

   ```bash
   brew install trivy gitleaks
   trivy --version
   gitleaks version
   ```

   `uv sync` installs Bandit through the `security-agent` extra. Restart Terminal 1
   after installation if its running server does not see the new commands. Create
   a new workflow after installing scanners; an already failed task is not retried.

5. In the portal, enter a real GitHub repository URL, its **existing** branch, an application
   name, and an environment. Select **Review plan**, then **Start source check**.
   The status page refreshes automatically. Local mode starts an in-process worker;
   the task should leave `QUEUED` and finish as `COMPLETED` or `FAILED` with output.
   Use a repository you can access; `acme/billing` in the API example is a placeholder.
   Branch names vary by repository: some use `main`, others use `master`.

6. Run the automated tests in **Terminal 2** if you also want to verify the
   application code:

   ```bash
   uv run --no-sync pytest -q
   ```

### If the local site cannot be reached

- Check that Terminal 1 still shows the Uvicorn process and its `127.0.0.1:8000`
  address. Start it again if it stopped or printed an error.
- Run `lsof -nP -iTCP:8000 -sTCP:LISTEN`. If nothing is listed, no server is
  listening on port 8000. If another process owns the port, stop that process in
  its terminal and restart `main.py`.
- Use `http://127.0.0.1:8000/` exactly, including `http` rather than `https`.
  `127.0.0.1` works only on the Mac running the server.
- If `/health` works but `/` returns an error, restart the server so it loads
  the latest portal code, then check Terminal 1 for the traceback.
- If cloning fails, confirm the repository URL and branch on GitHub. For a public
  repository, `git ls-remote https://github.com/OWNER/REPO.git HEAD refs/heads/BRANCH`
  should list the branch. For a private repository, configure `GITHUB_TOKEN`.

The local security worker requires `git`, `bandit`, `trivy`, and `gitleaks` to
perform a real source check. The Docker image includes the scanners. If a scanner
is missing locally, the workflow reports `FAILED` with `Scanner unavailable` in
its output. A `FAILED` result containing a JSON report with `"outcome":"blocked"`
means the scanners ran and found issues to review. Local workflow state is lost
when the API restarts. Local mode has no authentication and must stay on a
developer machine.

### Web portal scope

The portal has two flows: source checks and application deployment. Deployment
planning inspects a pinned GitHub commit and chooses a supported blueprint. A
root `Dockerfile` uses CodeBuild, ECR, the shared ECS cluster and load balancer,
and a separate CloudFront URL. A supported root `template.yaml` using the AWS
SAM transform uses an isolated CodeBuild project, a Lambda function, an on-demand
DynamoDB table, and an IAM-protected Lambda Function URL. The current SAM adapter
is intentionally narrow: one Python 3.12 `Api` function with `CodeUri: build/`,
a root `build.sh`, IAM URL authentication, and only the resource and IAM shapes
accepted by `sam_blueprint.py`. Other templates are rejected during planning.

The plan shows the blueprint, pinned commit, resources, cost drivers, and
prerequisites. An administrator approves it before any build or resource
creation. The SAM build runs without deployment permissions. The worker checks
the generated CloudFormation change set's resource types before execution and
invokes Lambda with an invalid request as a no-model-cost smoke check. A
`RUNNING` status confirms deployment and this smoke check; it does not verify
Bedrock model access or S3 Vectors retrieval. IAM-protected Function URLs require
SigV4 and return 403 when opened directly in an unsigned browser request.
Deletion removes the application stack and its ECR repository or SAM build
artifact, as applicable.

In AWS mode, the portal uses the Cognito managed login authorization code flow
with PKCE. Operators can create plans; readers can view plans and status;
administrators can approve and delete. Only GitHub repositories in
`ALLOWED_REPOSITORIES` are accepted. For a Dockerfile app, the platform infers
the port from a single `EXPOSE` and the health path from a local Dockerfile
health URL when possible; otherwise enter them. For a SAM app, the port and
container health path are unused. The SAM blueprint does not create an S3
Vectors bucket or ingest documents. Bedrock access, optional provider secrets,
and private retrieval prerequisites must be reviewed for that repository.
Review a repository before allowlisting it: its build script runs in CodeBuild.
Source checks are separate from deployment approval, so review their results before
approving a plan. In local mode you can preview a plan; approval and AWS actions
require the deployed AWS platform.

To try the flow locally, start the API as described above, open the portal,
and enter an allowlisted GitHub repository under **Application deployment**.
The branch defaults to `main`, and the app name is inferred from the repository.
Select **Create resource plan** and verify the commit, blueprint, stack name,
resources, prerequisites, and billable steps. The **Approve and deploy** button
appears only on the AWS platform. There, sign in as an `agents-admins` user,
approve the plan, and refresh until the status is `RUNNING`. For a container app,
open the HTTPS URL. For a SAM app, use a SigV4-signed client to invoke its URL.
Select **Delete app resources** when finished and wait for `DELETED`. If the plan reaches `FAILED`,
inspect its error and the CodeBuild build ID or CloudFormation stack events.

The existing workflow API still supports configured CI/CD and existing-stack
operations for operators.

## AWS layout

The Python CDK stack in `infra/app.py` provisions a VPC, private ECS Fargate
services for the API, dispatcher, result consumer, five agents, and the app
deployment worker, an HTTPS
Application Load Balancer in production. Staging uses a CloudFront managed
HTTPS domain and VPC origin to reach a private HTTP Application Load Balancer.
Both environments also provision RDS PostgreSQL, encrypted FIFO SQS queues with a
dead-letter queue, ECR, an app source S3 bucket, container and SAM CodeBuild projects,
Secrets Manager, CloudWatch Logs, and a Cognito user
pool. `migrations/` contains the Alembic schema. The task outbox commits with
workflow state; the dispatcher publishes pending tasks to SQS. Task claims and
saved results protect against duplicate deliveries. Workers obtain AWS
credentials from their task roles.

The integration hub exposes four authenticated, read-only HTTP tools under
`/integrations/tools`. It is an MCP-style connector hub, not a protocol MCP
server. `agents_shared` contains the Pydantic contracts and broker adapter.

## AWS release setup

### AWS access options

- **Local setup:** AWS CLI 2.32.0 or later supports `aws login`, which opens an
  interactive sign-in and supplies short-lived credentials to the CLI and SDK.
  Verify the selected account with `aws sts get-caller-identity` before any
  bootstrap or deployment command. Keep local credentials out of this repository.
- **GitHub deployment:** Configure the GitHub Actions workflow to assume a
  repository-scoped AWS IAM role through OIDC. Set `AWS_DEPLOY_ROLE_ARN` in the
  selected GitHub environment. Do not put AWS access keys in GitHub secrets.

1. Choose an AWS account and Region. Staging uses a temporary CloudFront HTTPS
   URL; no domain registration, Route 53 zone, or ACM certificate is needed.
   Production still requires a public Route 53 hosted zone, API domain, and an
   ACM certificate for that domain in the load balancer's Region. Run `aws login`
   and `gh auth login`, then use the one-time setup command:

   ```bash
   .venv/bin/python scripts/aws_build.py setup --region us-east-1 --environment staging --execute
   ```

   If a previous `CDKToolkit` stack was deleted but its asset bucket remains,
   run `scripts/aws_cleanup.py --mode bootstrap-assets --check` with `--region`
   and `--account-id`, then its `--execute` mode after reviewing the named
   assets. Setup refuses to bootstrap over that bucket.
2. Create `staging` and, if needed,
   `production` GitHub environments. Protect production with environment
   reviewers and restrict deployment branches.
3. Configure both environments' variables `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`,
   `OPENAI_MODEL`, `ALLOWED_REPOSITORIES`, `ALLOWED_PIPELINES`, and
   `ALLOWED_STACKS`. The three allowlists are comma-separated. For staging,
   also set `CLOUDFRONT_PREFIX_LIST_ID` to the AWS-managed
   `com.amazonaws.global.cloudfront.origin-facing` prefix list ID in the selected
   Region. Find it with `aws ec2 describe-managed-prefix-lists --region us-east-1
   --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing`.
   Production instead needs `CERTIFICATE_ARN`, `API_DOMAIN`, and `HOSTED_ZONE_ID`.
   Set environment secrets `AGENTS_GITHUB_TOKEN` and `AGENTS_OPENAI_API_KEY`.
4. The setup command creates the repository-scoped GitHub OIDC role, bootstraps
   CDK, and sets `AWS_DEPLOY_ROLE_ARN` and `AWS_REGION` in the GitHub environment.
   Configure production environment reviewers separately.
5. If agents manage CodePipeline or CloudFormation stacks, set CDK context
   `codepipeline_arns` and `managed_stack_arns` in `cdk.json` to the specific
   resources and grant the worker roles any additional permissions needed by
   their change sets. Review those permissions before enabling deploy targets.
6. Push `main` to run tests, then run the manual `Deploy AWS` workflow for
   staging. The release creates the stack
   with services at zero, builds and pushes the image, runs the migration task,
   starts services, and checks `GET /health` at the stack's `ApiDomain` output.
   In staging this is a `*.cloudfront.net` name. Use the manual `Deploy AWS`
   workflow for production. Confirm ECS service stability, SQS dead-letter
   counts, and an authenticated workflow before accepting the release.

The release starts one API task in staging and two in production. Dispatcher,
result consumer, and five agent services each start one task. To pause background
work in staging, set its GitHub environment variable
`BACKGROUND_DESIRED_COUNT=0` and redeploy. Workflows will queue but will not
advance until you set it back to `1` and redeploy. Production requires `1`.

The build entry point is `scripts/aws_build.py`. GitHub Actions calls its `deploy`
command. It restores the previous image
and desired count when the post-deploy health check fails. Database migrations
must be backward compatible because an image rollback does not reverse schema
changes.

### Complete AWS teardown

`scripts/aws_cleanup.py` is the single deletion entry point. Its default mode
prints a plan and makes no AWS calls:

```bash
.venv/bin/python scripts/aws_cleanup.py --region us-east-1 --account-id 123456789012
```

Choose `--mode stack` for the platform stack and all its managed application
stacks (staging or production),
`--mode all` for staging plus `CDKToolkit` and any report-backed leftovers, or
`--mode leftovers` after the staging stack was already deleted. The default is
`all` for staging. Add `--check` for a read-only AWS inventory, then `--execute`
when ready to delete. For the old staging deletion report, keep the JSON locally
and pass `--report PATH` if it is outside the project directory. A live stack's
database and KMS IDs are captured before teardown, so a report is unnecessary
for a fresh deletion. The script
checks the account, shows a CDK diff for live stacks, and requires exact typed
confirmations. The script deletes managed application stacks and their ECR
repositories before the platform stack. Bootstrap deletion refuses to run while
another CloudFormation stack is active in the account and Region. Leftover cleanup includes RDS
snapshots and retained backups, the CDK asset bucket, staging GitHub OIDC role
and provider, and the queue KMS key. KMS deletion has a mandatory waiting
period; manually created resources outside these scopes require separate review.

If `CDKToolkit` is gone but its default asset bucket or repository remains,
`setup` will refuse to bootstrap. Check those assets with `--mode
bootstrap-assets --check`; use `--mode bootstrap-assets --execute` only after
reviewing the exact bucket and repository shown. This mode requires no active
CloudFormation stacks and leaves the GitHub OIDC role, RDS backups, and other
leftovers alone.

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
