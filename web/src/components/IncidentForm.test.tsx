import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ClientConfig, Incident } from "../lib/types";
import { IncidentForm } from "./IncidentForm";

const config: ClientConfig = {
  llm_mode: "fake",
  can_simulate_failures: true,
  auth_required: false,
  orchestrator_model: "o",
  worker_model: "w",
  workers: [
    { name: "log_analysis", description: "logs" },
    { name: "comms_drafter", description: "comms" },
  ],
};
const samples: Incident[] = [
  {
    id: "disk",
    title: "Disk full",
    severity: "sev3",
    service: "reporting-batch",
    description: "The disk is full.",
    source: "monitoring",
    started_at: "2026-01-01T00:00:00Z",
  },
  { id: "cert", title: "Cert expiring", severity: "sev2", description: "Expires soon." },
];

function setup(overrides: Partial<Parameters<typeof IncidentForm>[0]> = {}) {
  const onSubmit = vi.fn();
  const onStop = vi.fn();
  render(
    <IncidentForm
      config={config}
      samples={samples}
      running={false}
      onSubmit={onSubmit}
      onStop={onStop}
      {...overrides}
    />,
  );
  return { onSubmit, onStop, user: userEvent.setup() };
}

describe("IncidentForm", () => {
  it("starts on the first sample, ready to submit", () => {
    setup();
    expect(screen.getByLabelText("Title")).toHaveValue("Disk full");
    expect(screen.getByLabelText("Severity")).toHaveValue("sev3");
  });

  it("fills the fields from a chosen sample", async () => {
    const { user } = setup();
    await user.selectOptions(screen.getByLabelText("Start from a sample"), "cert");
    expect(screen.getByLabelText("Title")).toHaveValue("Cert expiring");
    expect(screen.getByLabelText("Severity")).toHaveValue("sev2");
    expect(screen.getByLabelText(/Service/)).toHaveValue("");
  });

  it("submits what the user edited", async () => {
    const { user, onSubmit } = setup();
    const title = screen.getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Custom incident");
    await user.click(screen.getByRole("button", { name: "Respond to incident" }));
    const [request, token] = onSubmit.mock.calls[0] ?? [];
    expect(request.incident.title).toBe("Custom incident");
    expect(request.incident.description).toBe("The disk is full.");
    expect(request.simulate_failures).toEqual([]);
    expect(token).toBeUndefined();
  });

  it("keeps the sample's id, source and start time on submit", async () => {
    const { user, onSubmit } = setup();
    await user.click(screen.getByRole("button", { name: "Respond to incident" }));
    expect(onSubmit.mock.calls[0]?.[0].incident).toMatchObject({
      id: "disk",
      source: "monitoring",
      started_at: "2026-01-01T00:00:00Z",
    });
  });

  it("does not submit without a title and description", async () => {
    const { user, onSubmit } = setup();
    await user.clear(screen.getByLabelText("Title"));
    await user.click(screen.getByRole("button", { name: "Respond to incident" }));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("sends a chosen simulated failure", async () => {
    const { user, onSubmit } = setup();
    await user.selectOptions(screen.getByLabelText("Simulate a worker failure"), "log_analysis");
    await user.click(screen.getByRole("button", { name: "Respond to incident" }));
    expect(onSubmit.mock.calls[0]?.[0].simulate_failures).toEqual(["log_analysis"]);
  });

  it("offers no failure simulation with real models", () => {
    setup({ config: { ...config, llm_mode: "bedrock", can_simulate_failures: false } });
    expect(screen.queryByLabelText("Simulate a worker failure")).not.toBeInTheDocument();
  });

  it("asks for an access token only when the server needs one", async () => {
    const { user, onSubmit } = setup({ config: { ...config, auth_required: true } });
    const field = screen.getByLabelText("Access token");
    expect(field).toHaveAttribute("type", "password");
    await user.type(field, "secret");
    await user.click(screen.getByRole("button", { name: "Respond to incident" }));
    expect(onSubmit.mock.calls[0]?.[1]).toBe("secret");
  });

  it("hides the access token field otherwise", () => {
    setup();
    expect(screen.queryByLabelText("Access token")).not.toBeInTheDocument();
  });

  it("offers Stop instead of Respond while a run is in progress", async () => {
    const { user, onStop } = setup({ running: true });
    expect(screen.queryByRole("button", { name: "Respond to incident" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
  });
});
