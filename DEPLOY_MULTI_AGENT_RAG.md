# Deploy `multi_agent_rag` through DevOps Agent

Checked against `main` on 21 November 2026. This is a self-service runbook; writing it created no AWS resources.

## Readiness snapshot

| Item | State |
| --- | --- |
| Platform code | On `main`; since this path was verified it has gained a Dockerfile path, app settings, secret references and task sizes, which apply to container apps only |
| Platform on AWS | **Torn down** to save cost (about $2-3 per day while running). Rebuild it with the **Deploy AWS** workflow (step 2) |
| SAM path on AWS | Verified end to end: the platform built and deployed `multi_agent_rag` with an IAM-protected Function URL, then deleted it with **Delete app resources** |
| Staging allowlist | `ALLOWED_REPOSITORIES` already contains `Enterprise_AI_Model_Router` and `multi_agent_rag` (confirm with `gh variable list`) |
| Required GitHub config | Variables `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `CLOUDFRONT_PREFIX_LIST_ID`, `OPENAI_MODEL`; secrets `AGENTS_GITHUB_TOKEN`, `AGENTS_OPENAI_API_KEY` |
| Target source | `https://github.com/karitselmuthu/multi_agent_rag`, branch `main`; root `template.yaml` and `build.sh` match the restricted SAM blueprint |
| Target stack | Lambda, on-demand DynamoDB table, IAM-protected Lambda Function URL, and execution role |
| Outside the target stack | Bedrock model availability and permissions; S3 Vectors bucket, index, and documents |

The target repository does **not** need a Dockerfile, HTTP port, health path, task size, settings or secret references for this SAM path; a SAM app configures itself in `template.yaml`, and the platform refuses settings for it. It also does not need a separate branch. The portal pins the exact target commit in the plan.

## 1. Check the account and expected cost

Use a non-root AWS profile (for example `aws login --profile devops-agent-operator`). Confirm the account is `902510603662` and the Region is `us-east-1`:

```bash
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json
aws cloudformation describe-stacks --stack-name AgentsPlatform-staging --region us-east-1 \
  --query 'Stacks[0].StackStatus' --output text
```

If the platform stack is absent (the usual case now), the workflow creates the full platform, including a NAT gateway, RDS, load balancer and Fargate tasks. These cost money while they run, so delete the platform when you finish (step 5). A monthly budget alert is a good safety net. Approving an app later starts a CodeBuild job and creates the app stack; check the proposed resource plan before approval.

## 2. Release the DevOps Agent code

1. Confirm `main` contains `src/agents_platform/sam_blueprint.py` and the SAM additions in `infra/app.py`.
2. Confirm the `staging` environment settings (values of secrets are not shown):

   ```bash
   gh variable list --env staging --repo karitselmuthu/Devops_Agent
   gh secret list --env staging --repo karitselmuthu/Devops_Agent
   ```

   `ALLOWED_REPOSITORIES` must include `karitselmuthu/multi_agent_rag`. To add repositories, list them all, comma-separated, because setting the variable replaces its value:

   ```bash
   gh variable set ALLOWED_REPOSITORIES --env staging --repo karitselmuthu/Devops_Agent \
     --body 'karitselmuthu/Enterprise_AI_Model_Router,karitselmuthu/multi_agent_rag'
   ```

3. In GitHub **Actions -> Deploy AWS -> Run workflow**, select branch `main` and environment `staging`, or run:

   ```bash
   gh workflow run deploy.yml --ref main -f environment=staging --repo karitselmuthu/Devops_Agent
   ```

4. Watch the run to completion (a fresh platform takes roughly 30 minutes). CI does not deploy AWS resources. If `BACKGROUND_DESIRED_COUNT` is `0`, set it to `1` and redeploy; the application worker will not process approved plans while background services are stopped.
5. Create a Cognito user for the portal, because a rebuilt platform starts with none. In the AWS console (or CLI), open the user pool from the `AgentsPlatform-staging` outputs, create a user, set a temporary password, and add the user to the `agents-admins` group. They set a new password at first sign-in.

## 3. Create and approve the application plan

Get the portal host from **CloudFormation -> AgentsPlatform-staging -> Outputs -> ApiDomain**, or:

```bash
aws cloudformation describe-stacks --stack-name AgentsPlatform-staging --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiDomain'].OutputValue | [0]" --output text
```

Open `https://<ApiDomain>/` and sign in as an `agents-admins` user. Optionally run a **source check** first: it runs Bandit, Trivy and Gitleaks, and any finding blocks it. Then, under **Application deployment**, enter:

| Portal field | Value |
| --- | --- |
| GitHub repository URL | `https://github.com/karitselmuthu/multi_agent_rag` |
| Branch | `main` |
| App name | `multi-agent-rag` (or leave blank for inference) |
| Environment | Staging |
| Container HTTP port, health path, Dockerfile path, Task size, Settings, Secret references | Leave blank or default; ignored for SAM |

Select **Create resource plan**. Confirm `Python SAM / Lambda`, the pinned commit, stack name `AgentsApp-staging-...`, Lambda, DynamoDB, CodeBuild, S3 artifact, IAM, Bedrock usage and the listed prerequisites. Planning creates no AWS resources. Select **Approve and deploy** only after reviewing the plan and charges.

## 4. Check the deployment

The portal moves through `BUILDING` and `DEPLOYING` to `RUNNING`. If it fails, use its **Build ID** in CodeBuild and its stack name in CloudFormation. `RUNNING` means the Lambda returned the expected 400 to an empty request; it does **not** prove that Bedrock or retrieval works.

The application URL is an IAM-protected Lambda Function URL. A normal browser visit returns 403. To call it, use an AWS identity with `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` and a SigV4-capable client such as `awscurl`. A Cognito portal login does not supply those AWS permissions. See the [AWS Function URL invocation guide](https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html).

The target template defaults to Bedrock inference profile `us.amazon.nova-lite-v1:0` and Titan Text Embeddings v2. Check that the account/Region and the Lambda role can use both. The portal does not offer SAM parameter overrides, so changing `ModelId` there is not supported. The target repository's [template](https://github.com/karitselmuthu/multi_agent_rag/blob/main/template.yaml) lists the exact defaults and IAM resources.

To test a real query through the AWS CLI after preparing Bedrock and S3 Vectors, copy the app stack name from the portal, then find its Lambda function:

```bash
APP_STACK_NAME='AgentsApp-staging-REPLACE_WITH_PLAN_SUFFIX'
FUNCTION_NAME=$(aws cloudformation describe-stack-resources --stack-name "$APP_STACK_NAME" --region us-east-1 \
  --query "StackResources[?LogicalResourceId=='Api'].PhysicalResourceId | [0]" --output text)
aws lambda invoke --function-name "$FUNCTION_NAME" --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"body":"{\"query\":\"Summarize my documents\"}"}' rag-response.json
cat rag-response.json
```

This invokes Lambda directly, so it checks the application without testing the Function URL's HTTP authentication. A real query can incur Bedrock and S3 Vectors charges.

For a document-grounded answer, separately create and populate the `multi-agent-rag-vectors` S3 Vectors bucket and `docs` index using the target repository's `app/vectors.py` and your own documents, with an identity that can create and write S3 Vectors resources (this uses Bedrock embeddings and incurs usage). The app stack neither creates nor deletes that bucket or index. Follow the target's [README](https://github.com/karitselmuthu/multi_agent_rag/blob/main/README.md).

## 5. Clean up after the test

1. In the portal, select **Delete app resources** and wait for `DELETED`. This removes the app stack and its SAM zip artifact. Separately inspect and delete any S3 Vectors bucket and index you created. A Lambda log group created by invocation may remain.
2. To stop the platform's running cost, inspect the cleanup scope, then execute it:

   ```bash
   .venv/bin/python scripts/aws_cleanup.py --mode stack --region us-east-1 \
     --account-id 902510603662 --check
   ```

   `--mode stack --execute` deletes the platform stack and its managed app stacks after typed confirmation. It is much broader than deleting one app; delete app resources first. Some resources are retained by design, so use `--mode leftovers` or `--mode all` for the rest. See [AWS_RESOURCE_FLOW.md](AWS_RESOURCE_FLOW.md) for the resource and cost boundaries.
