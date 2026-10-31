import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { beforeAll, describe, expect, it } from "vitest";
import { IncidentOrchestratorStack } from "../lib/incident-stack";

const OPUS = "us.anthropic.claude-opus-5-5";
const HAIKU = "us.anthropic.claude-haiku-5-5";

let template: Template;

type Statement = { Action: string | string[]; Resource: unknown; Condition?: unknown; Effect: string };

/** The roles this project defines. CDK's own deployment helpers have their own, narrow roles. */
function statementsOfPolicies(): Statement[] {
  return Object.entries(template.findResources("AWS::IAM::Policy"))
    .filter(([id]) => id.startsWith("Agent") || id.startsWith("Api"))
    .flatMap(([, policy]) => policy.Properties.PolicyDocument.Statement as Statement[]);
}

const actionsOf = (statement: Statement) => [statement.Action].flat();

function policyStatementsFor(actionPrefix: string): Statement[] {
  return statementsOfPolicies().filter((s) =>
    actionsOf(s).some((action) => action.startsWith(actionPrefix)),
  );
}

function apiEnvironment(): Record<string, unknown> {
  const functions = template.findResources("AWS::Lambda::Function", {
    Properties: { PackageType: "Image" },
  });
  const [api] = Object.values(functions);
  return api!.Properties.Environment.Variables;
}

beforeAll(() => {
  const webDist = mkdtempSync(join(tmpdir(), "web-dist-"));
  writeFileSync(join(webDist, "index.html"), "<!doctype html>");
  const app = new App();
  const stack = new IncidentOrchestratorStack(app, "Test", {
    env: { account: "111122223333", region: "us-east-1" },
    bedrockModelId: OPUS,
    bedrockWorkerModelId: HAIKU,
    repoRoot: resolve(__dirname, "../.."),
    webDistPath: webDist,
  });
  template = Template.fromStack(stack);
}, 120_000);

describe("the agent on AgentCore Runtime", () => {
  it("runs the configured models over HTTP in public network mode", () => {
    template.hasResourceProperties("AWS::BedrockAgentCore::Runtime", {
      ProtocolConfiguration: "HTTP",
      NetworkConfiguration: { NetworkMode: "PUBLIC" },
      EnvironmentVariables: Match.objectLike({
        LLM_MODE: "bedrock",
        BEDROCK_MODEL_ID: OPUS,
        BEDROCK_WORKER_MODEL_ID: HAIKU,
      }),
    });
  });

  it("can be assumed only by AgentCore, for this account", () => {
    template.hasResourceProperties("AWS::IAM::Role", {
      AssumeRolePolicyDocument: {
        Statement: [
          Match.objectLike({
            Principal: { Service: "bedrock-agentcore.amazonaws.com" },
            Condition: { StringEquals: { "aws:SourceAccount": "111122223333" } },
          }),
        ],
      },
    });
  });
});

describe("least privilege", () => {
  it("scopes every Bedrock invoke permission to the two configured models", () => {
    const invoke = policyStatementsFor("bedrock:InvokeModel");
    const resources = invoke.flatMap((s) => [s.Resource].flat());
    expect(resources.sort()).toEqual(
      [
        `arn:aws:bedrock:us-east-1:111122223333:inference-profile/${OPUS}`,
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5",
        `arn:aws:bedrock:us-east-1:111122223333:inference-profile/${HAIKU}`,
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5",
      ].sort(),
    );
  });

  it("only allows the foundation models through their inference profiles", () => {
    const models = policyStatementsFor("bedrock:InvokeModel").filter((s) =>
      JSON.stringify(s.Resource).includes("foundation-model"),
    );
    expect(models).toHaveLength(2);
    for (const statement of models) {
      expect(statement.Condition).toHaveProperty(["StringEquals", "bedrock:InferenceProfileArn"]);
    }
  });

  it("uses no wildcard actions, and no wildcard resources outside AWS's documented exceptions", () => {
    // These actions have no resource-level permissions, so AWS's own policy uses "*" for them.
    const allowedStar = new Set([
      "ecr:GetAuthorizationToken",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "cloudwatch:PutMetricData",
    ]);
    for (const statement of statementsOfPolicies()) {
      const actions = actionsOf(statement);
      expect(actions.join(",")).not.toMatch(/(^|,)\*$|:\*/);
      if (statement.Resource === "*") {
        for (const action of actions) expect(allowedStar).toContain(action);
      }
    }
  });

  it("limits metrics to the AgentCore namespace", () => {
    const [metrics] = policyStatementsFor("cloudwatch:PutMetricData");
    expect(metrics!.Condition).toEqual({
      StringEquals: { "cloudwatch:namespace": "bedrock-agentcore" },
    });
  });

  it("gives the API no access to Bedrock models: it can only call the agent", () => {
    const apiPolicies = Object.entries(template.findResources("AWS::IAM::Policy")).filter(([id]) =>
      id.startsWith("Api"),
    );
    const json = JSON.stringify(apiPolicies);
    expect(apiPolicies.length).toBeGreaterThan(0);
    expect(json).toContain("bedrock-agentcore:InvokeAgentRuntime");
    expect(json).not.toContain("bedrock:InvokeModel");
  });

  it("lets the API invoke only this runtime", () => {
    const [invoke] = policyStatementsFor("bedrock-agentcore:InvokeAgentRuntime");
    const resources = JSON.stringify(invoke!.Resource);
    expect(resources).toContain("AgentRuntime");
    expect(invoke!.Resource).not.toBe("*");
  });
});

describe("secrets", () => {
  it("generates the access token rather than embedding one", () => {
    template.hasResourceProperties("AWS::SecretsManager::Secret", {
      GenerateSecretString: Match.objectLike({ ExcludePunctuation: true }),
    });
  });

  it("passes the API a reference to the secret, never its value", () => {
    const environment = apiEnvironment();
    expect(environment).toHaveProperty("API_ACCESS_TOKEN_SECRET_ARN");
    expect(environment).not.toHaveProperty("API_ACCESS_TOKEN");
    expect(JSON.stringify(environment)).not.toContain("resolve:secretsmanager");
  });

  it("lets only the API read the secret", () => {
    const readers = policyStatementsFor("secretsmanager:GetSecretValue");
    expect(readers).toHaveLength(1);
    expect(readers[0]?.Resource).not.toBe("*");
  });
});

describe("the API on Lambda", () => {
  it("is called through a streaming function URL that requires IAM", () => {
    template.hasResourceProperties("AWS::Lambda::Url", {
      AuthType: "AWS_IAM",
      InvokeMode: "RESPONSE_STREAM",
    });
  });

  it("calls the agent on AgentCore, streaming", () => {
    const environment = apiEnvironment();
    expect(environment).toMatchObject({
      AGENT_BACKEND: "agentcore",
      LLM_MODE: "bedrock",
      AWS_LWA_INVOKE_MODE: "response_stream",
    });
    expect(environment).toHaveProperty("AGENT_RUNTIME_ARN");
  });

  it("runs on arm64 with a bounded number of concurrent runs", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      PackageType: "Image",
      Architectures: ["arm64"],
      ReservedConcurrentExecutions: 5,
    });
  });

  it("outlives the agent's own run deadline", () => {
    const functions = template.findResources("AWS::Lambda::Function", {
      Properties: { PackageType: "Image" },
    });
    const [api] = Object.values(functions);
    expect(api!.Properties.Timeout).toBeGreaterThan(300);
  });
});

describe("the site", () => {
  it("keeps the bucket private", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
  });

  it("signs requests to both origins with origin access control", () => {
    const controls = template.findResources("AWS::CloudFront::OriginAccessControl");
    const types = Object.values(controls).map(
      (control) => control.Properties.OriginAccessControlConfig.OriginAccessControlOriginType,
    );
    expect(types.sort()).toEqual(["lambda", "s3"]);
  });

  it("lets only this distribution invoke the function, by both required actions", () => {
    // CloudFront's docs require both lambda:InvokeFunctionUrl and lambda:InvokeFunction.
    const permissions = Object.values(template.findResources("AWS::Lambda::Permission")).filter(
      (p) => p.Properties.Principal === "cloudfront.amazonaws.com",
    );
    expect(permissions.map((p) => p.Properties.Action).sort()).toEqual([
      "lambda:InvokeFunction",
      "lambda:InvokeFunctionUrl",
    ]);
    for (const permission of permissions) {
      expect(permission.Properties.SourceArn).toBeDefined();
    }
  });

  it("routes /api/* to the API, uncached, and passes the access token through", () => {
    template.hasResourceProperties("AWS::CloudFront::Distribution", {
      DistributionConfig: Match.objectLike({
        DefaultRootObject: "index.html",
        CacheBehaviors: [
          Match.objectLike({
            PathPattern: "/api/*",
            ViewerProtocolPolicy: "https-only",
            AllowedMethods: Match.arrayWith(["POST"]),
            // Managed policies: CachingDisabled, AllViewerExceptHostHeader.
            CachePolicyId: "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
            OriginRequestPolicyId: "b689b0a8-53d0-40ab-baf2-68738e2966ac",
          }),
        ],
      }),
    });
  });
});

describe("outputs", () => {
  it("tells the deployer where the site and token are", () => {
    const outputs = template.findOutputs("*");
    expect(Object.keys(outputs)).toEqual(
      expect.arrayContaining(["SiteUrl", "AccessTokenSecretArn", "AgentRuntimeArn"]),
    );
  });
});
