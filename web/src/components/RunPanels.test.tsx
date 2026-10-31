import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { initialRunState, reduceRun, replay, type WorkerView } from "../lib/runState";
import { loadRun } from "../test/fixtures";
import { PlanPanel } from "./PlanPanel";
import { Timeline } from "./Timeline";
import { WorkerCard } from "./WorkerCard";

function worker(overrides: Partial<WorkerView> = {}): WorkerView {
  return {
    toolUseId: "a",
    worker: "log_analysis",
    turn: 1,
    task: "Examine the logs",
    status: "running",
    liveText: "",
    steps: [],
    output: "",
    error: null,
    usage: null,
    durationMs: null,
    ...overrides,
  };
}

describe("PlanPanel", () => {
  it("shows the rationale and says which workers run in parallel", () => {
    render(<PlanPanel state={replay(loadRun("run-complete"))} />);
    const steps = screen.getAllByRole("listitem");
    expect(steps[0]).toHaveTextContent("3 workers in parallel");
    expect(steps[0]).toHaveTextContent("log_analysis");
    expect(steps[0]).toHaveTextContent("Simulated plan");
    expect(steps[1]).toHaveTextContent("1 worker");
    expect(steps[1]).toHaveTextContent("comms_drafter");
    expect(steps[1]).not.toHaveTextContent("parallel");
  });

  it("copes with a turn that has no rationale", () => {
    const state = replay([
      {
        type: "plan_recorded",
        turn: 1,
        rationale: null,
        calls: [{ tool_use_id: "a", worker: "log_analysis", task: "t" }],
      },
    ]);
    render(<PlanPanel state={state} />);
    expect(screen.getByRole("listitem")).toHaveTextContent("log_analysis");
  });

  it("says when the orchestrator is still deciding", () => {
    const state = reduceRun(
      { ...initialRunState, phase: "running" },
      { type: "orchestrator_turn", turn: 1 },
    );
    render(<PlanPanel state={state} />);
    expect(screen.getByText(/deciding/i)).toBeInTheDocument();
  });
});

describe("WorkerCard", () => {
  it("shows a running worker's brief and live text", () => {
    render(<WorkerCard worker={worker({ liveText: "Looking at" })} />);
    expect(screen.getByRole("heading", { name: "log_analysis" })).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Examine the logs")).toBeInTheDocument();
    expect(screen.getByText("Looking at")).toBeInTheDocument();
  });

  it("lists the tool calls as they happen", () => {
    const step = {
      kind: "tool_call" as const,
      name: "search_logs",
      detail: '{"service":"payments-api"}',
      is_error: false,
      at_ms: 5,
    };
    render(<WorkerCard worker={worker({ steps: [step] })} />);
    expect(screen.getByText("search_logs")).toBeInTheDocument();
  });

  it("shows a finished worker's output, duration and token use", () => {
    render(
      <WorkerCard
        worker={worker({
          status: "ok",
          output: "Pool exhausted",
          durationMs: 1500,
          usage: { input_tokens: 100, output_tokens: 20 },
        })}
      />,
    );
    expect(screen.getByText("Pool exhausted")).toBeInTheDocument();
    expect(screen.getByText(/1\.5 s/)).toBeInTheDocument();
    expect(screen.getByText(/120 tokens/)).toBeInTheDocument();
  });

  it("states a failure in words, with the reason", () => {
    render(<WorkerCard worker={worker({ status: "failed", error: "log store unavailable" })} />);
    expect(screen.getByText(/Failed/)).toBeInTheDocument();
    expect(screen.getByText("log store unavailable")).toBeInTheDocument();
  });
});

describe("Timeline", () => {
  const report = replay(loadRun("run-complete")).report!;

  it("has a row per worker call", () => {
    render(<Timeline findings={report.worker_findings} totalMs={report.duration_ms} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
  });

  it("places bars proportionally, so overlap is visible", () => {
    const findings = [
      { ...report.worker_findings[0]!, worker: "a", started_ms: 0, duration_ms: 500 },
      { ...report.worker_findings[0]!, worker: "b", started_ms: 250, duration_ms: 250 },
    ];
    render(<Timeline findings={findings} totalMs={1000} />);
    const bar = within(screen.getAllByRole("listitem")[1]!).getByTestId("bar");
    expect(bar).toHaveStyle({ left: "25%", width: "25%" });
  });

  it("describes each bar in text for screen readers", () => {
    const findings = [
      { ...report.worker_findings[0]!, worker: "a", started_ms: 200, duration_ms: 1000 },
    ];
    render(<Timeline findings={findings} totalMs={2000} />);
    expect(screen.getByRole("listitem")).toHaveTextContent("started at 0.2 s, took 1.0 s");
  });

  it("copes with a run so fast its total is zero", () => {
    const findings = [
      { ...report.worker_findings[0]!, worker: "a", started_ms: 0, duration_ms: 0 },
    ];
    render(<Timeline findings={findings} totalMs={0} />);
    expect(screen.getByTestId("bar").style.left).toBe("0%");
  });
});
