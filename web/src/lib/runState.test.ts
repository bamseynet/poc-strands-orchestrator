import { describe, expect, it } from "vitest";
import { loadRun } from "../test/fixtures";
import { initialRunState, reduceRun, replay } from "./runState";
import type { RunEvent, WorkerRun } from "./types";

function workerRun(overrides: Partial<WorkerRun> = {}): WorkerRun {
  return {
    tool_use_id: "a",
    worker: "log_analysis",
    turn: 1,
    task: "t",
    status: "ok",
    output: "out",
    error: null,
    usage: { input_tokens: 1, output_tokens: 1 },
    trace: [],
    started_ms: 0,
    duration_ms: 1,
    ...overrides,
  };
}

const started: RunEvent = {
  type: "worker_started",
  tool_use_id: "a",
  worker: "log_analysis",
  turn: 1,
  task: "t",
};

describe("reduceRun", () => {
  it("records the plan in the order it was decided", () => {
    const state = replay([
      { type: "plan_recorded", turn: 1, rationale: "why", calls: [] },
      { type: "plan_recorded", turn: 2, rationale: null, calls: [] },
    ]);
    expect(state.plan.map((p) => p.turn)).toEqual([1, 2]);
  });

  it("keeps workers in the order they started", () => {
    const state = replay([started, { ...started, tool_use_id: "b", worker: "comms_drafter" }]);
    expect(state.workers.map((w) => w.toolUseId)).toEqual(["a", "b"]);
    expect(state.workers[0]?.status).toBe("running");
  });

  it("streams text into the right worker", () => {
    const state = replay([
      started,
      { ...started, tool_use_id: "b" },
      { type: "worker_text_delta", tool_use_id: "b", worker: "x", text: "hel" },
      { type: "worker_text_delta", tool_use_id: "b", worker: "x", text: "lo" },
    ]);
    expect(state.workers[0]?.liveText).toBe("");
    expect(state.workers[1]?.liveText).toBe("hello");
  });

  it("lets a later finish correct an earlier one", () => {
    const state = replay([
      started,
      { type: "worker_finished", run: workerRun({ status: "ok" }) },
      { type: "worker_finished", run: workerRun({ status: "failed", error: "late verdict" }) },
    ]);
    expect(state.workers).toHaveLength(1);
    expect(state.workers[0]?.status).toBe("failed");
    expect(state.workers[0]?.error).toBe("late verdict");
  });

  it("ignores events for a worker it never saw start", () => {
    const before = replay([started]);
    const after = reduceRun(before, {
      type: "worker_text_delta",
      tool_use_id: "ghost",
      worker: "x",
      text: "boo",
    });
    expect(after.workers).toEqual(before.workers);
  });

  it("ignores event types from a newer server", () => {
    const unknown = { type: "something_new" } as unknown as RunEvent;
    expect(reduceRun(initialRunState, unknown)).toEqual(initialRunState);
  });

  it("does not mutate the previous state", () => {
    const before = replay([started]);
    const snapshot = structuredClone(before);
    reduceRun(before, { type: "worker_text_delta", tool_use_id: "a", worker: "x", text: "hi" });
    expect(before).toEqual(snapshot);
  });

  it("marks a failed run", () => {
    const state = replay([{ type: "run_failed", error: "no credentials" }]);
    expect(state.phase).toBe("failed");
    expect(state.error).toBe("no credentials");
  });

  it("stops every running worker when the server gives up on the run", () => {
    const state = replay([started, { type: "run_failed", error: "boom" }]);
    expect(state.workers.every((w) => w.status !== "running")).toBe(true);
  });
});

describe("replaying real captured runs", () => {
  it("ends a complete run with its report and every worker finished", () => {
    const state = replay(loadRun("run-complete"));
    expect(state.phase).toBe("done");
    expect(state.report?.status).toBe("complete");
    expect(state.workers).toHaveLength(4);
    expect(state.workers.every((w) => w.status === "ok")).toBe(true);
    expect(state.plan[0]?.rationale).toContain("parallel");
  });

  it("captures each worker's tool calls as they happened", () => {
    const state = replay(loadRun("run-complete"));
    const logs = state.workers.find((w) => w.worker === "log_analysis");
    expect(logs?.steps.some((s) => s.name === "summarise_log_errors")).toBe(true);
    expect(logs?.steps.some((s) => s.kind === "tool_result")).toBe(true);
  });

  it("ends a degraded run with the failure attributed to its worker", () => {
    const state = replay(loadRun("run-degraded"));
    expect(state.phase).toBe("done");
    expect(state.report?.status).toBe("degraded");
    const logs = state.workers.find((w) => w.worker === "log_analysis");
    expect(logs?.status).toBe("failed");
    expect(logs?.error).toContain("Simulated failure");
    expect(state.workers.filter((w) => w.status === "ok")).toHaveLength(3);
  });

  it("shows the workers overlapping before any finishes", () => {
    const events = loadRun("run-complete");
    const firstFinish = events.findIndex((e) => e.type === "worker_finished");
    const running = replay(events.slice(0, firstFinish)).workers;
    expect(running).toHaveLength(3);
    expect(running.every((w) => w.status === "running")).toBe(true);
  });
});
