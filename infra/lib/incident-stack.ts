import { CfnOutput, Duration, IgnoreMode, RemovalPolicy, Stack, type StackProps } from "aws-cdk-lib";
import { CfnRuntime } from "aws-cdk-lib/aws-bedrockagentcore";
import {
  AllowedMethods,
  CachePolicy,
  Distribution,
  OriginRequestPolicy,
  ResponseHeadersPolicy,
  ViewerProtocolPolicy,
} from "aws-cdk-lib/aws-cloudfront";
import { FunctionUrlOrigin, S3BucketOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import { Effect, PolicyStatement, Role, ServicePrincipal } from "aws-cdk-lib/aws-iam";
import {
  Architecture,
  DockerImageCode,
  DockerImageFunction,
  FunctionUrlAuthType,
  InvokeMode,
} from "aws-cdk-lib/aws-lambda";
import { LogGroup, RetentionDays } from "aws-cdk-lib/aws-logs";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import { BucketDeployment, Source } from "aws-cdk-lib/aws-s3-deployment";
import { Secret } from "aws-cdk-lib/aws-secretsmanager";
import type { Construct } from "constructs";
import { bedrockInvokeStatements } from "./bedrock-iam";

const RUNTIME_NAME = "incident_orchestrator";
// Each concurrent run holds one Lambda instance and spends money, so this is the spending brake.
const MAX_CONCURRENT_RUNS = 5;

export interface IncidentOrchestratorStackProps extends StackProps {
  bedrockModelId: string;
  bedrockWorkerModelId: string;
  repoRoot: string;
  webDistPath: string;
}

/**
 * The agent on Bedrock AgentCore Runtime, and the API and UI in front of it.
 *
 *   browser -> CloudFront -> S3 (static UI)
 *                         -> Lambda function URL (API, streaming) -> AgentCore Runtime -> Bedrock
 *
 * Everything scales to zero, which suits a proof of concept: there is no idle compute to pay for.
 */
export class IncidentOrchestratorStack extends Stack {
  constructor(scope: Construct, id: string, props: IncidentOrchestratorStackProps) {
    super(scope, id, props);

    const runtime = this.agentRuntime(props);
    const { api, accessToken } = this.api(props, runtime);
    const siteUrl = this.site(props, api);

    new CfnOutput(this, "SiteUrl", { value: siteUrl });
    new CfnOutput(this, "AccessTokenSecretArn", {
      value: accessToken.secretArn,
      description: "Holds the access token the UI asks for",
    });
    new CfnOutput(this, "AgentRuntimeArn", { value: runtime.attrAgentRuntimeArn });
  }

  private agentRuntime(props: IncidentOrchestratorStackProps): CfnRuntime {
    const image = new DockerImageAsset(this, "AgentImage", {
      directory: props.repoRoot,
      file: "Dockerfile.agent",
      platform: Platform.LINUX_ARM64,
      ignoreMode: IgnoreMode.DOCKER,
    });

    // Based on the execution role in the AgentCore docs, scoped further: AWS's sample allows every
    // foundation model, and this allows exactly the two this agent uses.
    // https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
    const role = new Role(this, "AgentRole", {
      assumedBy: new ServicePrincipal("bedrock-agentcore.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": this.account },
          ArnLike: { "aws:SourceArn": `arn:aws:bedrock-agentcore:${this.region}:${this.account}:*` },
        },
      }),
    });
    image.repository.grantPull(role);

    const logGroups = `arn:aws:logs:${this.region}:${this.account}:log-group:/aws/bedrock-agentcore/runtimes`;
    const identityDirectory = `arn:aws:bedrock-agentcore:${this.region}:${this.account}:workload-identity-directory/default`;
    for (const statement of [
      ...bedrockInvokeStatements(this, props.bedrockModelId),
      ...bedrockInvokeStatements(this, props.bedrockWorkerModelId),
      new PolicyStatement({
        actions: ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
        resources: [`${logGroups}/*`],
      }),
      new PolicyStatement({
        actions: ["logs:CreateLogStream", "logs:PutLogEvents"],
        resources: [`${logGroups}/*:log-stream:*`],
      }),
      new PolicyStatement({
        // Lets AgentCore deliver this agent's spans to its own log group.
        actions: ["logs:PutResourcePolicy"],
        resources: [`${logGroups}/${RUNTIME_NAME}-*`],
      }),
      new PolicyStatement({
        actions: ["logs:DescribeLogGroups"],
        resources: [`arn:aws:logs:${this.region}:${this.account}:log-group:*`],
      }),
      new PolicyStatement({
        actions: [
          "xray:PutTraceSegments",
          "xray:PutTelemetryRecords",
          "xray:GetSamplingRules",
          "xray:GetSamplingTargets",
        ],
        resources: ["*"],
      }),
      new PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
        conditions: { StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" } },
      }),
      new PolicyStatement({
        // ForUserId, also in AWS's sample, is left out: this agent acts for no end users.
        actions: [
          "bedrock-agentcore:GetWorkloadAccessToken",
          "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        ],
        resources: [identityDirectory, `${identityDirectory}/workload-identity/${RUNTIME_NAME}-*`],
      }),
    ]) {
      role.addToPolicy(statement);
    }

    const runtime = new CfnRuntime(this, "AgentRuntime", {
      agentRuntimeName: RUNTIME_NAME,
      description: "Orchestrator-workers incident response agent",
      agentRuntimeArtifact: { containerConfiguration: { containerUri: image.imageUri } },
      networkConfiguration: { networkMode: "PUBLIC" },
      protocolConfiguration: "HTTP",
      roleArn: role.roleArn,
      environmentVariables: {
        LLM_MODE: "bedrock",
        BEDROCK_MODEL_ID: props.bedrockModelId,
        BEDROCK_WORKER_MODEL_ID: props.bedrockWorkerModelId,
        AWS_REGION: this.region,
      },
    });
    runtime.node.addDependency(role);
    return runtime;
  }

  private api(props: IncidentOrchestratorStackProps, runtime: CfnRuntime) {
    const accessToken = new Secret(this, "AccessToken", {
      description: "Required on POST /api/runs, because every run spends money",
      generateSecretString: { excludePunctuation: true, passwordLength: 40 },
    });

    const api = new DockerImageFunction(this, "Api", {
      code: DockerImageCode.fromImageAsset(props.repoRoot, {
        file: "Dockerfile.api",
        platform: Platform.LINUX_ARM64,
        ignoreMode: IgnoreMode.DOCKER,
      }),
      architecture: Architecture.ARM_64,
      memorySize: 512,
      // Longer than the agent's own 300s deadline, so the agent's report always gets through.
      timeout: Duration.minutes(6),
      reservedConcurrentExecutions: MAX_CONCURRENT_RUNS,
      logGroup: new LogGroup(this, "ApiLogs", {
        retention: RetentionDays.ONE_MONTH,
        removalPolicy: RemovalPolicy.DESTROY,
      }),
      environment: {
        AGENT_BACKEND: "agentcore",
        AGENT_RUNTIME_ARN: runtime.attrAgentRuntimeArn,
        API_ACCESS_TOKEN_SECRET_ARN: accessToken.secretArn,
        LLM_MODE: "bedrock",
        BEDROCK_MODEL_ID: props.bedrockModelId,
        BEDROCK_WORKER_MODEL_ID: props.bedrockWorkerModelId,
        AWS_LWA_INVOKE_MODE: "response_stream",
      },
    });
    accessToken.grantRead(api);
    api.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ["bedrock-agentcore:InvokeAgentRuntime"],
        resources: [runtime.attrAgentRuntimeArn, `${runtime.attrAgentRuntimeArn}/runtime-endpoint/*`],
      }),
    );
    return { api, accessToken };
  }

  private site(props: IncidentOrchestratorStackProps, api: DockerImageFunction): string {
    const bucket = new Bucket(this, "Site", {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // AWS_IAM plus origin access control: the only way in is through this distribution.
    const url = api.addFunctionUrl({
      authType: FunctionUrlAuthType.AWS_IAM,
      invokeMode: InvokeMode.RESPONSE_STREAM,
    });

    const distribution = new Distribution(this, "Distribution", {
      defaultRootObject: "index.html",
      defaultBehavior: {
        origin: S3BucketOrigin.withOriginAccessControl(bucket),
        viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy: ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      additionalBehaviors: {
        "/api/*": {
          // Heartbeats arrive every 15s, well inside this gap between bytes.
          origin: FunctionUrlOrigin.withOriginAccessControl(url, {
            readTimeout: Duration.seconds(60),
          }),
          viewerProtocolPolicy: ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: AllowedMethods.ALLOW_ALL,
          cachePolicy: CachePolicy.CACHING_DISABLED,
          originRequestPolicy: OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      },
    });

    new BucketDeployment(this, "SiteFiles", {
      sources: [Source.asset(props.webDistPath)],
      destinationBucket: bucket,
      distribution,
      distributionPaths: ["/*"],
    });

    return `https://${distribution.distributionDomainName}`;
  }
}
