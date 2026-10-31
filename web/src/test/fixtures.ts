import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { RunEvent } from "../lib/types";

/** A real run captured from the agent. See tests/test_web_contract.py. */
export function loadRun(name: "run-complete" | "run-degraded"): RunEvent[] {
  return readFileSync(resolve(__dirname, "fixtures", `${name}.jsonl`), "utf-8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line) as RunEvent);
}

/** Render events as the SSE body the API sends. */
export function toSse(events: RunEvent[]): string {
  return events.map((e) => `event: ${e.type}\ndata: ${JSON.stringify(e)}\n\n`).join("");
}

/** A byte stream of the given chunks, as fetch would deliver it. */
export function streamOf(...chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      }
      controller.close();
    },
  });
}
