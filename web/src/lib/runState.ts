import type {
  Incident,
  IncidentReport,
  PlannedCall,
  RunEvent,
  TraceStep,
  Usage,
  WorkerRun,
} from "./types";

export interface WorkerView {
  toolUseId: string;
  worker: string;
  turn: number;
  task: string;
  status: WorkerRun["status"];
  liveText: string;
  steps: TraceStep[];
  output: string;
  error: string | null;
  usage: Usage | null;
  durationMs: number | null;
}

export interface PlanEntry {
  turn: number;
  rationale: string | null;
  calls: PlannedCall[];
}

export interface RunState {
  phase: "idle" | "running" | "done" | "failed" | "stopped";
  incident: Incident | null;
  orchestratorModel: string | null;
  workerModel: string | null;
  turn: number;
  plan: PlanEntry[];
  workers: WorkerView[];
  synthesising: boolean;
  report: IncidentReport | null;
  error: string | null;
}

export const initialRunState: RunState = {
  phase: "idle",
  incident: null,
  orchestratorModel: null,
  workerModel: null,
  turn: 0,
  plan: [],
  workers: [],
  synthesising: false,
  report: null,
  error: null,
};

function updateWorker(
  state: RunState,
  toolUseId: string,
  change: (worker: WorkerView) => WorkerView,
): RunState {
  return {
    ...state,
    workers: state.workers.map((w) => (w.toolUseId === toolUseId ? change(w) : w)),
  };
}

export function reduceRun(state: RunState, event: RunEvent): RunState {
  switch (event.type) {
    case "run_started":
      return {
        ...initialRunState,
        phase: "running",
        incident: event.incident,
        orchestratorModel: event.orchestrator_model,
        workerModel: event.worker_model,
      };
    case "orchestrator_turn":
      return { ...state, turn: event.turn };
    case "plan_recorded":
      return {
        ...state,
        plan: [...state.plan, { turn: event.turn, rationale: event.rationale, calls: event.calls }],
      };
    case "worker_started":
      return {
        ...state,
        workers: [
          ...state.workers,
          {
            toolUseId: event.tool_use_id,
            worker: event.worker,
            turn: event.turn,
            task: event.task,
            status: "running",
            liveText: "",
            steps: [],
            output: "",
            error: null,
            usage: null,
            durationMs: null,
          },
        ],
      };
    case "worker_text_delta":
      return updateWorker(state, event.tool_use_id, (w) => ({
        ...w,
        liveText: w.liveText + event.text,
      }));
    case "worker_progress":
      return updateWorker(state, event.tool_use_id, (w) => ({
        ...w,
        steps: [...w.steps, event.step],
        // A completed text step supersedes what was streaming.
        liveText: event.step.kind === "text" ? "" : w.liveText,
      }));
    case "worker_finished":
      return updateWorker(state, event.run.tool_use_id, (w) => ({
        ...w,
        status: event.run.status,
        steps: event.run.trace,
        output: event.run.output,
        error: event.run.error,
        usage: event.run.usage,
        durationMs: event.run.duration_ms,
        liveText: "",
      }));
    case "synthesis_started":
      return { ...state, synthesising: true };
    case "report_ready":
      return { ...state, phase: "done", synthesising: false, report: event.report };
    case "run_failed":
      // Nothing is still running once the server has given up, so no worker may keep spinning.
      return endRunLocally(state, "failed", event.error);
    default:
      // Tolerate event types added by a newer server.
      return state;
  }
}

export const replay = (events: RunEvent[]): RunState => events.reduce(reduceRun, initialRunState);

/** The run ended on this side: a stop, or a lost connection. Nothing is still running after this. */
export function endRunLocally(
  state: RunState,
  phase: "stopped" | "failed",
  reason: string,
): RunState {
  return {
    ...state,
    phase,
    synthesising: false,
    error: phase === "failed" ? reason : null,
    workers: state.workers.map((w) =>
      w.status === "running" ? { ...w, status: "failed", error: reason, liveText: "" } : w,
    ),
  };
}
