import { useCallback, useEffect, useRef, useState } from "react";
import { startRun } from "./api";
import { endRunLocally, initialRunState, reduceRun, type RunState } from "./runState";
import type { RunRequest } from "./types";

const CONNECTION_LOST = "The connection dropped before the run finished, so it cannot be resumed.";

/** Runs one incident at a time, folding its events into state as they arrive. */
export function useRun() {
  const [state, setState] = useState<RunState>(initialRunState);
  const current = useRef<AbortController | null>(null);

  useEffect(() => () => current.current?.abort(), []);

  const start = useCallback(async (request: RunRequest, accessToken?: string) => {
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    setState({ ...initialRunState, phase: "running" });

    let finished = false;
    try {
      const events = await startRun(request, { accessToken, signal: controller.signal });
      for await (const event of events) {
        if (controller.signal.aborted) break;
        setState((previous) => reduceRun(previous, event));
        finished ||= event.type === "report_ready" || event.type === "run_failed";
      }
      if (!finished && !controller.signal.aborted) {
        setState((previous) => endRunLocally(previous, "failed", CONNECTION_LOST));
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = error instanceof Error ? error.message : String(error);
      const reason = error instanceof TypeError ? CONNECTION_LOST : message;
      setState((previous) => endRunLocally(previous, "failed", reason));
    }
  }, []);

  const stop = useCallback(() => {
    // Aborting closes the connection, which is the server's signal to stop the agents.
    current.current?.abort();
    setState((previous) =>
      previous.phase === "running"
        ? endRunLocally(previous, "stopped", "Stopped before it finished.")
        : previous,
    );
  }, []);

  return { state, start, stop };
}
