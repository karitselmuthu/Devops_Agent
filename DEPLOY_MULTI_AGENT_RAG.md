# Deploy `multi_agent_rag` through DevOps Agent

Checked against the pushed branches on 20 November 2026. This is a self-service runbook; writing it created no AWS resources.

## Readiness snapshot

| Item | State |
| --- | --- |
| DevOps Agent deployment code | Draft [PR #5](https://github.com/karitselmuthu/Devops_Agent/pull/5) on `feature/sam-repository-deployment`; CI `verify` passed |
| AWS deployment workflow | Runs from `main`; the setup code restricts the staging GitHub OIDC role and environment branch policy to `main` (verify live settings before release) |
| Staging allowlist | Currently contains only `karitselmuthu/Enterprise_AI_Model_Router`; add `karitselmuthu/multi_agent_rag` |
| Target source | `https://github.com/karitselmuthu/multi_agent_rag`, branch `main`; root `template.yaml` and `build.sh` match the restricted SAM blueprint |
| Target stack | Lambda, on-demand DynamoDB table, IAM-protected Lambda Function URL, and execution role |
| Outside the target stack | Bedrock model availability and permissions; S3 Vectors bucket, index, and documents |
| SAM AWS integration test | Not yet performed; passing CI and CDK synthesis do not verify live IAM, CodeBuild, or CloudFormation behavior |
| Live AWS state | Not checked for this report; verify the account and platform stack yourself before deploying |

The target repository does **not** need a Dockerfile, HTTP port, or container health path for this SAM path. It also does not need a separate branch: the deployment-specific code is in DevOps Agent, and the target's existing `main` template is used as source. The portal pins the exact target commit in the plan.

## 1. Check the account and expected cost

Use a non-root AWS profile. Confirm that the account is `902510603662` and Region is `us-east-1`:

```bash
aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json
aws cloudformation describe-stacks --stack-name AgentsPlatform-staging --region us-east-1 \
  --query 'Stacks[0].StackStatus' --output text
```

If the platform stack is absent, the workflow will create the full platform, including NAT gateway, RDS, load balancer, and Fargate tasks. Those have ongoing charges while running. If it exists, the workflow updates it and adds the SAM build project and scoped role. Application approval later starts a CodeBuild job and creates the application stack. Check the proposed resource plan before approval.

## 2. Release the DevOps Agent code

1. Open [PR #5](https://github.com/karitselmuthu/Devops_Agent/pull/5), review the changes and passing CI, mark the draft ready, and merge it into `main`.
2. Confirm the `main` branch now contains `src/agents_platform/sam_blueprint.py` and the SAM additions in `infra/app.py`.
3. In the `staging` GitHub environment, add the target repository while retaining the existing allowed repository:

   ```bash
   gh variable set ALLOWED_REPOSITORIES --env staging --repo karitselmuthu/Devops_Agent \
     --body 'karitselmuthu/Enterprise_AI_Model_Router,karitselmuthu/multi_agent_rag'
   gh variable list --env staging --repo karitselmuthu/Devops_Agent
   gh secret list --env staging --repo karitselmuthu/Devops_Agent
   ```

   Verify `AWS_REGION=us-east-1`, `AWS_DEPLOY_ROLE_ARN`, `CLOUDFRONT_PREFIX_LIST_ID`, and `OPENAI_MODEL`. The secret list must contain `AGENTS_GITHUB_TOKEN` and `AGENTS_OPENAI_API_KEY`; their values are not displayed. The release script requires both secrets.

4. In GitHub **Actions → Deploy AWS → Run workflow**, select branch `main` and environment `staging`. Or run:

   ```bash
   gh workflow run deploy.yml --ref main -f environment=staging --repo karitselmuthu/Devops_Agent
   ```

5. Watch that GitHub Actions run to completion. A green CI check on PR #5 does not deploy AWS resources. If `BACKGROUND_DESIRED_COUNT` is `0`, set it to `1` and redeploy; the application worker will not process approved plans while background services are stopped.

## 3. Create and approve the application plan

Get the platform portal host from **CloudFormation → AgentsPlatform-staging → Outputs → ApiDomain**, or:

```bash
aws cloudformation describe-stacks --stack-name AgentsPlatform-staging --region us-east-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiDomain'].OutputValue | [0]" --output text
```

Open `https://<ApiDomain>/`, sign in as a Cognito user in `agents-admins`, then enter:

| Portal field | Value |
| --- | --- |
| GitHub repository URL | `https://github.com/karitselmuthu/multi_agent_rag` |
| Branch | `main` |
| App name | `multi-agent-rag` (or leave blank for inference) |
| Environment | Staging |
| Container HTTP port | Leave blank |
| Container health check path | Ignored for SAM |

Select **Create resource plan**. Confirm `Python SAM / Lambda`, the pinned commit, stack name `AgentsApp-staging-...`, Lambda, DynamoDB, CodeBuild, S3 artifact, IAM, Bedrock usage, and the listed prerequisites. Planning itself creates no AWS resources. Select **Approve and deploy** only after reviewing the plan and charges.

## 4. Check the deployment

The portal should move through `BUILDING` and `DEPLOYING` to `RUNNING`. Use its **Build ID** in CodeBuild and its stack name in CloudFormation if it fails. `RUNNING` means the Lambda returned the expected 400 response to an empty request; it does **not** prove that Bedrock or retrieval works.

The application URL is an IAM-protected Lambda Function URL. A normal browser visit returns 403. To call it, use an AWS identity with `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` and a SigV4-capable client. A Cognito portal login alone does not supply those AWS permissions. See the [AWS Function URL invocation guide](https://docs.aws.amazon.com/lambda/latest/dg/urls-invocation.html).

The target template defaults to Bedrock inference profile `us.amazon.nova-lite-v1:0` and Titan Text Embeddings v2. Check that the account/Region and the Lambda role can use both. The portal currently does not offer SAM parameter overrides, so changing `ModelId` in the portal is not supported. The target repository's [template](https://github.com/karitselmuthu/multi_agent_rag/blob/main/template.yaml) contains the exact defaults and IAM resource list.

To test a real query through AWS CLI after preparing Bedrock and S3 Vectors, copy the app stack name shown in the portal, then find its Lambda function:

```bash
APP_STACK_NAME='AgentsApp-staging-REPLACE_WITH_PLAN_SUFFIX'
FUNCTION_NAME=$(aws cloudformation describe-stack-resources --stack-name "$APP_STACK_NAME" --region us-east-1 \
  --query "StackResources[?LogicalResourceId=='Api'].PhysicalResourceId | [0]" --output text)
aws lambda invoke --function-name "$FUNCTION_NAME" --region us-east-1 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"body":"{\"query\":\"Summarize my documents\"}"}' rag-response.json
cat rag-response.json
```

This invokes Lambda directly, so it checks the application without testing the Function URL's HTTP authentication. A real query can incur Bedrock and S3 Vectors charges. For a signed HTTP URL test, use the target repository's `awscurl` example with an authorized AWS profile.

For an actual document-grounded answer, separately create and populate the `multi-agent-rag-vectors` S3 Vectors bucket and `docs` index using the target repository's `app/vectors.py` and your own documents. This ingestion uses Bedrock embeddings and incurs AWS usage. The DevOps Agent application stack does not create or delete that bucket/index. Follow the target repository's [RAG ingest instructions](https://github.com/karitselmuthu/multi_agent_rag/blob/main/README.md) with an AWS identity that can create and write S3 Vectors resources.

## 5. Clean up after the test

In the portal, select **Delete app resources** and wait for `DELETED`. This removes the app stack and its SAM zip artifact. Separately inspect and delete any S3 Vectors bucket/index you created. A CloudWatch Lambda log group created by invocation may remain after the app stack is deleted.

If you also want to remove the staging platform, first inspect the cleanup scope:

```bash
.venv/bin/python scripts/aws_cleanup.py --mode stack --region us-east-1 \
  --account-id 902510603662 --check
```

`--mode stack --execute` then deletes the platform stack and its managed app stacks after typed confirmation. It is much broader than deleting one app. See [AWS_RESOURCE_FLOW.md](AWS_RESOURCE_FLOW.md) for the resource and cost boundaries.
