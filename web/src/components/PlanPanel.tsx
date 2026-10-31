import type { RunState } from "../lib/runState";

export function PlanPanel({ state }: { state: RunState }) {
  const deciding =
    state.phase === "running" &&
    !state.synthesising &&
    state.workers.every((worker) => worker.status !== "running");

  return (
    <section aria-labelledby="plan-heading" className="plan">
      <h2 id="plan-heading">Orchestrator's plan</h2>
      {state.plan.length > 0 && (
        <ol className="plan-steps">
          {state.plan.map((entry, index) => (
            <li key={index}>
              <span className="plan-turn">Turn {entry.turn}</span>
              {entry.calls.length > 0 && (
                <p className="plan-summary">
                  {entry.calls.length} {entry.calls.length === 1 ? "worker" : "workers"}
                  {entry.calls.length > 1 && " in parallel"}:{" "}
                  {entry.calls.map((call) => call.worker).join(", ")}
                </p>
              )}
              {entry.rationale && <p className="plan-rationale">{entry.rationale}</p>}
            </li>
          ))}
        </ol>
      )}
      {deciding && <p className="muted pulse">Deciding what to do next…</p>}
      {state.synthesising && <p className="muted pulse">Writing the report from the findings…</p>}
    </section>
  );
}
