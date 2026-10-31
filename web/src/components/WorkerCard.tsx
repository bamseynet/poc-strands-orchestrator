import { count, seconds } from "../lib/format";
import type { WorkerView } from "../lib/runState";
import { StatusChip } from "./StatusChip";

export function WorkerCard({ worker }: { worker: WorkerView }) {
  const calls = worker.steps.filter((step) => step.kind === "tool_call");
  return (
    <article className={`worker worker-${worker.status}`}>
      <header>
        <h3>{worker.worker}</h3>
        <StatusChip status={worker.status} />
      </header>
      <p className="worker-task">{worker.task}</p>
      {calls.length > 0 && (
        <ul className="worker-calls">
          {calls.map((call, index) => (
            <li key={index}>
              <code>{call.name}</code>
            </li>
          ))}
        </ul>
      )}
      {worker.liveText && <p className="worker-live">{worker.liveText}</p>}
      {worker.output && <p className="worker-output">{worker.output}</p>}
      {worker.error && <p className="worker-error">{worker.error}</p>}
      {worker.durationMs !== null && (
        <p className="worker-meta">
          {seconds(worker.durationMs)}
          {worker.usage &&
            ` · ${count(worker.usage.input_tokens + worker.usage.output_tokens)} tokens`}
        </p>
      )}
    </article>
  );
}
