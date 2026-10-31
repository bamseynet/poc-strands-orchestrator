// Mirrors src/incident_agent/schemas.py. tests/test_web_contract.py fails when the agent's events
// change shape, and the fixtures it maintains are replayed through this code in the tests here.

export type Severity = "sev1" | "sev2" | "sev3" | "sev4";

export interface Incident {
  id?: string;
  title: string;
  severity: Severity;
  service?: string | null;
  description: string;
  source?: string | null;
  started_at?: string | null;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
}

export interface TraceStep {
  kind: "text" | "tool_call" | "tool_result";
  name: string | null;
  detail: string;
  is_error: boolean;
  at_ms: number;
}

export interface WorkerRun {
  tool_use_id: string;
  worker: string;
  turn: number;
  task: string;
  status: "running" | "ok" | "failed";
  output: string;
  error: string | null;
  usage: Usage;
  trace: TraceStep[];
  started_ms: number;
  duration_ms: number | null;
}

export interface RecommendedAction {
  action: string;
  priority: "now" | "soon" | "later";
}

export interface CostLine {
  label: string;
  model_id: string;
  input_tokens: number;
  output_tokens: number;
  usd: number | null;
}

export interface IncidentReport {
  run_id: string;
  incident: Incident;
  status: "complete" | "degraded";
  synthesis_source: "orchestrator" | "fallback";
  summary: string;
  probable_cause: string;
  confidence: "low" | "medium" | "high";
  recommended_actions: RecommendedAction[];
  comms_draft: string | null;
  worker_findings: WorkerRun[];
  unacknowledged_workers: string[];
  degradation_notes: string[];
  cost: { lines: CostLine[]; total_usd: number | null; basis: string };
  duration_ms: number;
}

export interface PlannedCall {
  tool_use_id: string;
  worker: string;
  task: string;
}

export type RunEvent =
  | {
      type: "run_started";
      run_id: string;
      incident: Incident;
      orchestrator_model: string;
      worker_model: string;
      available_workers: string[];
    }
  | { type: "orchestrator_turn"; turn: number }
  | { type: "plan_recorded"; turn: number; rationale: string | null; calls: PlannedCall[] }
  | { type: "worker_started"; tool_use_id: string; worker: string; turn: number; task: string }
  | { type: "worker_text_delta"; tool_use_id: string; worker: string; text: string }
  | { type: "worker_progress"; tool_use_id: string; worker: string; step: TraceStep }
  | { type: "worker_finished"; run: WorkerRun }
  | { type: "synthesis_started" }
  | { type: "report_ready"; report: IncidentReport }
  | { type: "run_failed"; error: string };

export interface ClientConfig {
  llm_mode: "fake" | "bedrock";
  can_simulate_failures: boolean;
  auth_required: boolean;
  orchestrator_model: string;
  worker_model: string;
  workers: { name: string; description: string }[];
}

export interface RunRequest {
  incident: Incident;
  simulate_failures: string[];
}
