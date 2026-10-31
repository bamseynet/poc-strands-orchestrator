import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { replay } from "../lib/runState";
import type { IncidentReport } from "../lib/types";
import { loadRun } from "../test/fixtures";
import { ReportView } from "./ReportView";

const complete = replay(loadRun("run-complete")).report!;
const degraded = replay(loadRun("run-degraded")).report!;

describe("ReportView", () => {
  it("shows the synthesised conclusions", () => {
    render(<ReportView report={complete} />);
    expect(screen.getByText(complete.summary)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Probable cause/ }).nextElementSibling).toHaveTextContent(
      "connection pool exhausted",
    );
    expect(screen.getByText(/medium confidence/i)).toBeInTheDocument();
    const actions = screen.getByRole("heading", { name: "Recommended actions" }).nextElementSibling;
    expect(actions).toHaveTextContent("deployctl rollback");
  });

  it("gives every worker's output its own section", () => {
    render(<ReportView report={complete} />);
    for (const finding of complete.worker_findings) {
      const section = screen.getByRole("group", { name: new RegExp(finding.worker) });
      expect(section).toBeInTheDocument();
    }
  });

  it("does not claim problems on a complete report", () => {
    render(<ReportView report={complete} />);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("states clearly when a report is degraded, and why", () => {
    render(<ReportView report={degraded} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/incomplete/i);
    expect(alert).toHaveTextContent("log_analysis");
  });

  it("flags a failed worker in its own section with the reason", () => {
    render(<ReportView report={degraded} />);
    const section = screen.getByRole("group", { name: /log_analysis/ });
    expect(section).toHaveTextContent(/failed/i);
    expect(section).toHaveTextContent("Simulated failure");
  });

  it("flags when the fallback stood in for the orchestrator's synthesis", () => {
    const report: IncidentReport = {
      ...degraded,
      synthesis_source: "fallback",
      degradation_notes: ["The run hit its deadline."],
    };
    render(<ReportView report={report} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/did not finish its synthesis/i);
  });

  it("exposes each worker's trace of real tool calls", () => {
    render(<ReportView report={complete} />);
    const section = screen.getByRole("group", { name: /log_analysis/ });
    expect(within(section).getAllByText("summarise_log_errors").length).toBeGreaterThan(0);
  });

  it("shows the comms draft and copies it", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    render(<ReportView report={complete} />);
    await user.click(screen.getByRole("button", { name: "Copy draft" }));
    expect(writeText).toHaveBeenCalledWith(complete.comms_draft);
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("omits comms when the orchestrator did not ask for any", () => {
    render(<ReportView report={{ ...complete, comms_draft: null }} />);
    expect(screen.queryByRole("heading", { name: /comms/i })).not.toBeInTheDocument();
  });

  it("itemises tokens and cost per model tier and labels it as an estimate", () => {
    render(<ReportView report={complete} />);
    const table = screen.getByRole("table", { name: /estimate/i });
    const rows = within(table).getAllByRole("row");
    expect(rows.some((r) => within(r).queryByText("orchestrator"))).toBe(true);
    expect(rows.some((r) => within(r).queryByText("log_analysis"))).toBe(true);
    expect(within(table).getByText(/Total/)).toBeInTheDocument();
  });

  it("never shows a made-up cost for a model it cannot price", () => {
    const line = { ...complete.cost.lines[0]!, usd: null };
    const report = { ...complete, cost: { ...complete.cost, lines: [line], total_usd: null } };
    render(<ReportView report={report} />);
    const table = screen.getByRole("table", { name: /estimate/i });
    expect(within(table).getAllByText("unknown").length).toBe(2);
    expect(table).not.toHaveTextContent("$");
  });

  it("renders model output as inert text, never as HTML", () => {
    const hostile = '<img src=x onerror="alert(1)">';
    const report = { ...complete, summary: hostile, comms_draft: hostile };
    const { container } = render(<ReportView report={report} />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByText(hostile).length).toBe(2);
  });
});
