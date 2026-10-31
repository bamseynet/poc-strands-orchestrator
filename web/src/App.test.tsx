import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { ClientConfig, Incident, RunEvent } from "./lib/types";
import { loadRun, streamOf, toSse } from "./test/fixtures";

const config: ClientConfig = {
  llm_mode: "fake",
  can_simulate_failures: true,
  auth_required: false,
  orchestrator_model: "us.anthropic.claude-opus-5-5",
  worker_model: "us.anthropic.claude-haiku-5-5",
  workers: [{ name: "log_analysis", description: "Reads logs." }],
};
const samples: Incident[] = [
  { id: "s", title: "Checkout failing", severity: "sev1", description: "503s" },
];

function serve(runResponse: () => Response | Promise<Response>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/config") return Response.json(config);
      if (url === "/api/samples") return Response.json(samples);
      return runResponse();
    }),
  );
}

const respond = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole("button", { name: "Respond to incident" }));

beforeEach(() => {
  vi.stubGlobal("scrollTo", vi.fn());
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("explains the demo before a run, and says when the LLM is fake", async () => {
    serve(() => new Response(null));
    render(<App />);
    expect(await screen.findByText(/rule-based fake/i)).toBeInTheDocument();
    expect(screen.getByText("Reads logs.")).toBeInTheDocument();
  });

  it("does not claim the LLM is fake with real models", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input) === "/api/config"
          ? Response.json({ ...config, llm_mode: "bedrock", can_simulate_failures: false })
          : Response.json(samples),
      ),
    );
    render(<App />);
    await screen.findByLabelText("Title");
    expect(screen.queryByText(/rule-based fake/i)).not.toBeInTheDocument();
    expect(screen.getByText(/claude-opus-5-5/)).toBeInTheDocument();
  });

  it("takes a real captured run from submission to report", async () => {
    const user = userEvent.setup();
    serve(() => new Response(streamOf(toSse(loadRun("run-complete")))));
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);

    expect(await screen.findByRole("heading", { name: "Incident report" })).toBeInTheDocument();
    expect(screen.getByText(/3 workers in parallel/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/report ready/i);
    expect(screen.getByRole("button", { name: "Respond to incident" })).toBeEnabled();
  });

  it("shows workers progressing while the stream is still open", async () => {
    const user = userEvent.setup();
    const events = loadRun("run-complete");
    const firstFinish = events.findIndex((e) => e.type === "worker_finished");
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const encoder = new TextEncoder();
    serve(
      () =>
        new Response(
          new ReadableStream({
            async start(controller) {
              controller.enqueue(encoder.encode(toSse(events.slice(0, firstFinish))));
              await gate;
              controller.enqueue(encoder.encode(toSse(events.slice(firstFinish))));
              controller.close();
            },
          }),
        ),
    );
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);

    await waitFor(() => expect(screen.getAllByText("Running").length).toBe(3));
    expect(screen.getByRole("status")).toHaveTextContent(/3 workers running/i);
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();

    release();
    expect(await screen.findByRole("heading", { name: "Incident report" })).toBeInTheDocument();
  });

  it("shows a degraded run as finished, with the worker's failure", async () => {
    const user = userEvent.setup();
    serve(() => new Response(streamOf(toSse(loadRun("run-degraded")))));
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/incomplete/i);
  });

  it("shows why a run was refused, and allows a retry", async () => {
    const user = userEvent.setup();
    serve(() => Response.json({ detail: "Too many runs in progress." }, { status: 429 }));
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many runs");
    expect(screen.getByRole("button", { name: "Respond to incident" })).toBeEnabled();
  });

  it("shows a run that failed on the server", async () => {
    const user = userEvent.setup();
    const failed: RunEvent = { type: "run_failed", error: "The orchestrator failed: no access" };
    serve(() => new Response(streamOf(toSse([failed]))));
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("no access");
  });

  it("says so when the connection drops before the run ends", async () => {
    const user = userEvent.setup();
    const partial = loadRun("run-complete").slice(0, 3);
    serve(() => new Response(streamOf(toSse(partial))));
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);
    expect(await screen.findByRole("alert")).toHaveTextContent(/connection/i);
  });

  it("stopping closes the stream and keeps what was found", async () => {
    const user = userEvent.setup();
    const events = loadRun("run-complete");
    const firstFinish = events.findIndex((e) => e.type === "worker_finished");
    const encoder = new TextEncoder();
    serve(
      () =>
        new Response(
          new ReadableStream({
            start(controller) {
              controller.enqueue(encoder.encode(toSse(events.slice(0, firstFinish))));
            },
          }),
        ),
    );
    render(<App />);
    await screen.findByLabelText("Title");
    await respond(user);
    await waitFor(() => expect(screen.getAllByText("Running").length).toBe(3));

    await user.click(screen.getByRole("button", { name: "Stop" }));

    const fetchMock = vi.mocked(fetch);
    const runCall = fetchMock.mock.calls.find(([url]) => url === "/api/runs");
    expect(runCall?.[1]?.signal?.aborted).toBe(true);
    expect(screen.getByRole("status")).toHaveTextContent(/stopped/i);
    expect(screen.getByRole("heading", { name: "log_analysis" })).toBeInTheDocument();
    expect(screen.queryByText("Running")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Respond to incident" })).toBeEnabled();
  });

  it("says so when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<App />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not reach the api/i);
  });
});
