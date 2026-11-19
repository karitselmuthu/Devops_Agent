# AWS resource flow

This diagram describes the current staging architecture in `infra/app.py` and the application stack created by `src/agents_platform/app_aws.py`. Each deployed application gets its own CloudFormation stack, ECS service, target group, listener rule, and CloudFront distribution; it shares the platform VPC, cluster, and load balancer.

```mermaid
flowchart LR
    User[Operator browser] --> PortalCF[CloudFront HTTPS portal]
    PortalCF --> ALB[Internal application load balancer]
    User --> Cognito[Cognito user pool]
    Cognito -->|Sign-in tokens| User

    subgraph VPC[Platform VPC: public and private subnets]
        ALB --> API[API portal ECS Fargate service]
        API --> RDS[(RDS PostgreSQL 16)]
        API --> SQS[(SQS task and app deploy queues)]
        SQS --> Dispatcher[Dispatcher and results ECS services]
        SQS --> Agents[Code, security, DevOps, infrastructure, monitoring ECS workers]
        SQS --> AppWorker[Application deploy ECS worker]
        Dispatcher --> RDS
        Agents --> RDS
        AppWorker --> RDS
        NAT[NAT gateway] --> Internet[GitHub and AWS public APIs]
        Agents --> NAT
        AppWorker --> NAT
    end

    Secrets[Secrets Manager: GitHub token, OpenAI key, DB credentials] --> API
    Secrets --> Agents
    Logs[CloudWatch Logs] <-->|Container logs| API
    Logs <-->|Container logs| Agents
    Logs <-->|Container logs| AppWorker

    GitHub[GitHub application repository] -->|Source archive| AppWorker
    AppWorker -->|Upload source| S3[S3 app source bucket]
    AppWorker -->|Start build| CodeBuild[CodeBuild container build]
    S3 --> CodeBuild
    CodeBuild --> ECR[ECR application image repository]
    AppWorker -->|Create or update approved stack| CFN[CloudFormation application stack]
    CFN --> AppCF[CloudFront HTTPS application URL]
    CFN --> Rule[ALB listener rule and target group]
    CFN --> AppECS[Application ECS Fargate service]
    AppCF -->|VPC origin and route header| ALB
    ALB --> Rule --> AppECS
    ECR --> AppECS
    AppECS --> Logs

    PlatformRepo[GitHub platform repository] --> Actions[GitHub Actions deployment]
    Actions -->|OIDC assume role| CDK[AWS CDK / CloudFormation platform stack]
    CDK --> VPC
    CDK --> PortalCF
    CDK --> Cognito
    CDK --> RDS
    CDK --> SQS
    CDK --> Secrets
    CDK --> CodeBuild
```

## Deployment and status flow

1. The operator signs in through Cognito, submits a GitHub repository and target settings, and reviews the generated resource plan in the portal.
2. After approval, the application deploy worker uploads source to S3, starts CodeBuild, and pushes the resulting image to ECR.
3. The worker creates or updates the application's CloudFormation stack. ECS starts the container and the worker checks its configured health path before reporting success.
4. The portal reads application state from the platform database. GitHub Actions and CloudFormation record platform deployment status; CodeBuild, CloudFormation, ECS events, and CloudWatch Logs record application deployment details.

## Cost and cleanup boundaries

- The platform keeps a NAT gateway, RDS instance, load balancer, and Fargate services running while staging is up. These are ongoing costs even when no application is deployed.
- Each deployed application adds a Fargate service and CloudFront distribution, with associated usage and storage charges. CodeBuild charges for builds; ECR and S3 charge for retained data.
- Delete application stacks and their ECR repositories through the portal or project cleanup tooling. The platform cleanup command removes the platform stack and managed application resources. Check retained data and AWS billing after cleanup.
- To reduce chat usage, watch GitHub Actions for platform deployments and use the portal's status page plus AWS Console events and logs for application deployments. Chat polling is unnecessary for routine status tracking.
