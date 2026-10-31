import { type Stack } from "aws-cdk-lib";
import { Effect, PolicyStatement } from "aws-cdk-lib/aws-iam";

const INVOKE_ACTIONS = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"];
// An optional geo or global prefix makes this an inference profile id. Exact models only.
const MODEL_ID = /^(?:(us|ca|eu|au|jp|global)\.)?(anthropic\.claude-[a-z0-9-]+)$/;

/**
 * Least-privilege access to one model.
 *
 * A cross-Region inference profile needs permission on the profile and on the model in every
 * destination Region. Those destinations can change, so the Region is a wildcard, but the condition
 * means the model can only be reached through this one profile.
 * https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html
 */
export function bedrockInvokeStatements(stack: Stack, modelId: string): PolicyStatement[] {
  const match = MODEL_ID.exec(modelId);
  if (!match) {
    throw new Error(`Not a Claude model id or inference profile id: ${JSON.stringify(modelId)}`);
  }
  const [, geo, model] = match;
  if (!geo) {
    return [
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: INVOKE_ACTIONS,
        resources: [`arn:aws:bedrock:${stack.region}::foundation-model/${model}`],
      }),
    ];
  }

  const profile = `arn:aws:bedrock:${stack.region}:${stack.account}:inference-profile/${modelId}`;
  const models = [`arn:aws:bedrock:*::foundation-model/${model}`];
  if (geo === "global") {
    models.push(`arn:aws:bedrock:::foundation-model/${model}`);
  }
  return [
    new PolicyStatement({ effect: Effect.ALLOW, actions: INVOKE_ACTIONS, resources: [profile] }),
    new PolicyStatement({
      effect: Effect.ALLOW,
      actions: INVOKE_ACTIONS,
      resources: models,
      conditions: { StringEquals: { "bedrock:InferenceProfileArn": profile } },
    }),
  ];
}
