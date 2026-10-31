import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { App } from "aws-cdk-lib";
import { IncidentOrchestratorStack } from "../lib/incident-stack";

const repoRoot = resolve(import.meta.dirname, "../..");
const webDistPath = resolve(repoRoot, "web/dist");
if (!existsSync(webDistPath)) {
  throw new Error("web/dist is missing. Run `npm --prefix web run build`, or use `make synth`.");
}

const app = new App();
new IncidentOrchestratorStack(app, "IncidentOrchestrator", {
  description: "Strands Agents orchestrator-workers proof of concept for incident response",
  bedrockModelId: app.node.getContext("bedrockModelId"),
  bedrockWorkerModelId: app.node.getContext("bedrockWorkerModelId"),
  repoRoot,
  webDistPath,
});
