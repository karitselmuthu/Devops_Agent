#!/usr/bin/env bash
# One-time setup: lets GitHub Actions in this repo deploy to AWS without stored keys.
# Usage:  bash scripts/setup_aws_oidc.sh <aws-region> [staging|production]
# Run from your Mac terminal with AWS admin credentials and `gh auth login` done.
# Safe to re-run: every step checks for existing resources first.
set -euo pipefail

REGION="${1:?Usage: bash scripts/setup_aws_oidc.sh <aws-region> [staging|production]}"
ENVIRONMENT="${2:-staging}"
REPO="karitselmuthu/Devops_Agent"
STACK="AgentsPlatform-${ENVIRONMENT}"
CDK_VERSION="2.1148.1"   # same version scripts/deploy_aws.py uses
case "$ENVIRONMENT" in
  staging) ROLE_NAME="DevopsAgentGitHubStaging" ;;
  production) ROLE_NAME="DevopsAgentGitHubProduction" ;;
  *) echo "Environment must be staging or production"; exit 1 ;;
esac

step() { printf '\n== %s\n' "$1"; }

step "0. Checking tools and logins"
for tool in aws gh npx; do
  command -v "$tool" >/dev/null || { echo "Missing '$tool'. Install it first (brew install awscli gh node)."; exit 1; }
done
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
gh auth status >/dev/null
OWNER="$(gh api "repos/${REPO}" --jq .owner.login)"
OWNER_ID="$(gh api "repos/${REPO}" --jq .owner.id)"
REPO_NAME="$(gh api "repos/${REPO}" --jq .name)"
REPO_ID="$(gh api "repos/${REPO}" --jq .id)"
SUBJECT="repo:${OWNER}@${OWNER_ID}/${REPO_NAME}@${REPO_ID}:environment:${ENVIRONMENT}"
echo "AWS account : ${ACCOUNT_ID}"
echo "Region      : ${REGION}"
echo "OIDC subject: ${SUBJECT}"
read -r -p "Create/update IAM and GitHub settings with these values? [y/N] " answer
[ "$answer" = "y" ] || { echo "Stopped. Nothing changed."; exit 0; }

WORK="$(mktemp -d)"
PROVIDER_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

step "1. GitHub identity provider in AWS"
if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "$PROVIDER_ARN" >/dev/null 2>&1; then
  echo "Already exists."
else
  aws iam create-open-id-connect-provider \
    --url https://token.actions.githubusercontent.com \
    --client-id-list sts.amazonaws.com >/dev/null
  echo "Created."
fi

step "2. IAM role ${ROLE_NAME} (who may assume it)"
cat > "$WORK/trust.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "${PROVIDER_ARN}"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "${SUBJECT}",
        "token.actions.githubusercontent.com:ref": "refs/heads/main"
      }
    }
  }]
}
JSON
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam update-assume-role-policy --role-name "$ROLE_NAME" --policy-document "file://$WORK/trust.json"
  echo "Role existed; trust policy updated."
else
  aws iam create-role --role-name "$ROLE_NAME" \
    --description "GitHub Actions deploy role for ${REPO} (${ENVIRONMENT})" \
    --assume-role-policy-document "file://$WORK/trust.json" >/dev/null
  echo "Created."
fi

step "3. Deployment permissions (what the role may do)"
cat > "$WORK/permissions.json" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "AssumeCdkBootstrapRoles", "Effect": "Allow", "Action": "sts:AssumeRole",
     "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/cdk-hnb659fds-*-${ACCOUNT_ID}-${REGION}"},
    {"Sid": "ReadStack", "Effect": "Allow", "Action": "cloudformation:DescribeStacks",
     "Resource": "arn:aws:cloudformation:${REGION}:${ACCOUNT_ID}:stack/${STACK}/*"},
    {"Sid": "WriteAppSecrets", "Effect": "Allow", "Action": "secretsmanager:PutSecretValue",
     "Resource": ["arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:GitHubToken*",
                  "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:OpenAIKey*"]},
    {"Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
    {"Sid": "EcrPush", "Effect": "Allow",
     "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
     "Resource": "arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/agentsplatform-${ENVIRONMENT}-*"},
    {"Sid": "RunMigrationTask", "Effect": "Allow", "Action": "ecs:RunTask",
     "Resource": "arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task-definition/AgentsPlatform*:*"},
    {"Sid": "WatchMigrationTask", "Effect": "Allow", "Action": "ecs:DescribeTasks",
     "Resource": "arn:aws:ecs:${REGION}:${ACCOUNT_ID}:task/${STACK}-*/*"},
    {"Sid": "PassTaskRoles", "Effect": "Allow", "Action": "iam:PassRole",
     "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${STACK}-*",
     "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}}
  ]
}
JSON
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name deploy --policy-document "file://$WORK/permissions.json"
echo "Attached inline policy 'deploy'."

step "4. CDK bootstrap in ${ACCOUNT_ID}/${REGION}"
if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" >/dev/null 2>&1; then
  echo "Already bootstrapped."
else
  npx --yes "aws-cdk@${CDK_VERSION}" bootstrap "aws://${ACCOUNT_ID}/${REGION}"
fi

step "5. GitHub environment '${ENVIRONMENT}' (main branch only)"
echo '{"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}' \
  | gh api -X PUT "repos/${REPO}/environments/${ENVIRONMENT}" --input - >/dev/null
gh api -X POST "repos/${REPO}/environments/${ENVIRONMENT}/deployment-branch-policies" \
  -f name=main -f type=branch >/dev/null 2>&1 || echo "(main branch rule already present)"
gh variable set AWS_DEPLOY_ROLE_ARN --env "$ENVIRONMENT" --repo "$REPO" --body "$ROLE_ARN"
gh variable set AWS_REGION --env "$ENVIRONMENT" --repo "$REPO" --body "$REGION"

rm -rf "$WORK"
step "Done"
echo "Role ARN: ${ROLE_ARN}"
echo "Next: push .github/workflows/verify-oidc.yml, then run:"
echo "  gh workflow run 'Verify AWS OIDC' --repo ${REPO} -f environment=${ENVIRONMENT}"
