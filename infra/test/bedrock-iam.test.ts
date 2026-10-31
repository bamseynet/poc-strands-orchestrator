import { Stack } from "aws-cdk-lib";
import { describe, expect, it } from "vitest";
import { bedrockInvokeStatements } from "../lib/bedrock-iam";

const stack = new Stack(undefined, "T", { env: { account: "111122223333", region: "us-east-1" } });
const render = (modelId: string) =>
  bedrockInvokeStatements(stack, modelId).map((statement) => statement.toStatementJson());

const PROFILE = "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-opus-5-5";

describe("bedrockInvokeStatements", () => {
  it("allows invoking the inference profile itself", () => {
    const [profile] = render("us.anthropic.claude-opus-5-5");
    expect(profile.Resource).toBe(PROFILE);
    expect(profile.Action).toEqual(["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]);
  });

  it("allows the underlying model in the destination Regions, only through that profile", () => {
    const [, model] = render("us.anthropic.claude-opus-5-5");
    expect(model.Resource).toBe("arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5");
    expect(model.Condition).toEqual({ StringEquals: { "bedrock:InferenceProfileArn": PROFILE } });
  });

  it("names exact models, with no wildcard in the model id", () => {
    const json = JSON.stringify(render("eu.anthropic.claude-haiku-5-5"));
    expect(json).toContain("foundation-model/anthropic.claude-haiku-5-5");
    expect(json).not.toContain("foundation-model/*");
    expect(json).not.toContain("inference-profile/*");
  });

  it("handles a global profile, whose destinations have no Region in their ARN", () => {
    const [, model] = render("global.anthropic.claude-opus-5-5");
    expect(model.Resource).toEqual([
      "arn:aws:bedrock:*::foundation-model/anthropic.claude-opus-5-5",
      "arn:aws:bedrock:::foundation-model/anthropic.claude-opus-5-5",
    ]);
  });

  it("scopes a bare model id to the stack's Region, with no profile involved", () => {
    const statements = render("anthropic.claude-opus-5-5");
    expect(statements).toHaveLength(1);
    expect(statements[0].Resource).toBe(
      "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-5-5",
    );
    expect(statements[0].Condition).toBeUndefined();
  });

  it.each(["", "*", "us.anthropic.*", "not a model id", "us.amazon.nova-pro"])(
    "refuses %j rather than produce a broad or broken policy",
    (modelId) => {
      expect(() => render(modelId)).toThrow(/model id/i);
    },
  );
});
