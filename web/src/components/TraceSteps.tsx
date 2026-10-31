import type { TraceStep } from "../lib/types";

const LABEL: Record<TraceStep["kind"], string> = {
  text: "Said",
  tool_call: "Called",
  tool_result: "Got",
};

/** What a worker really did. All of this is untrusted text, and is rendered as text. */
export function TraceSteps({ steps }: { steps: TraceStep[] }) {
  return (
    <ol className="trace">
      {steps.map((step, index) => (
        <li key={index} className={step.is_error ? "trace-error" : undefined}>
          <span className="trace-kind">{step.is_error ? "Error" : LABEL[step.kind]}</span>
          {step.name && <code className="trace-name">{step.name}</code>}
          {step.kind !== "text" && <pre>{step.detail}</pre>}
          {step.kind === "text" && <p>{step.detail}</p>}
        </li>
      ))}
    </ol>
  );
}
