import { useState } from "react";
import { count, seconds, usd } from "../lib/format";
import type { CostLine, IncidentReport } from "../lib/types";
import { StatusChip } from "./StatusChip";
import { Timeline } from "./Timeline";
import { TraceSteps } from "./TraceSteps";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(text);
    setCopied(true);
  }
  return (
    <button type="button" className="secondary small" onClick={() => void copy()}>
      {copied ? "Copied" : "Copy draft"}
    </button>
  );
}

function CostRow({ line }: { line: CostLine }) {
  return (
    <tr>
      <th scope="row">{line.label}</th>
      <td>
        <code>{line.model_id}</code>
      </td>
      <td className="num">{count(line.input_tokens)}</td>
      <td className="num">{count(line.output_tokens)}</td>
      <td className="num">{usd(line.usd)}</td>
    </tr>
  );
}

export function ReportView({ report }: { report: IncidentReport }) {
  return (
    <section aria-labelledby="report-heading" className="report">
      <h2 id="report-heading">Incident report</h2>

      {report.status === "degraded" && (
        <div role="alert" className="banner banner-warning">
          <strong>This report is incomplete.</strong>
          {report.synthesis_source === "fallback" && (
            <p>The orchestrator did not finish its synthesis, so rely on the findings below.</p>
          )}
          <ul>
            {report.degradation_notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}

      <h3>Summary</h3>
      <p>{report.summary}</p>

      <h3>
        Probable cause <span className="muted">({report.confidence} confidence)</span>
      </h3>
      <p className="preserve-lines">{report.probable_cause}</p>

      {report.recommended_actions.length > 0 && (
        <>
          <h3>Recommended actions</h3>
          <ol className="actions">
            {report.recommended_actions.map((action) => (
              <li key={action.action}>
                <span className={`priority priority-${action.priority}`}>{action.priority}</span>
                {action.action}
              </li>
            ))}
          </ol>
          <p className="muted small-print">
            Recommendations only. Nothing in this system changes your infrastructure.
          </p>
        </>
      )}

      {report.comms_draft && (
        <>
          <div className="heading-row">
            <h3>Stakeholder comms draft</h3>
            <CopyButton text={report.comms_draft} />
          </div>
          <blockquote className="preserve-lines">{report.comms_draft}</blockquote>
        </>
      )}

      {report.worker_findings.length > 0 && (
        <>
          <h3>How the work overlapped</h3>
          <Timeline findings={report.worker_findings} totalMs={report.duration_ms} />
          <p className="muted small-print">Total run time {seconds(report.duration_ms)}.</p>

          <h3>Worker findings and traces</h3>
          <p className="muted small-print">
            Each worker's own output, taken directly from the run rather than from the
            orchestrator's summary.
          </p>
          {report.worker_findings.map((finding) => (
            <details
              key={finding.tool_use_id}
              className="finding"
              role="group"
              aria-label={finding.worker}
            >
              <summary>
                <code>{finding.worker}</code> <StatusChip status={finding.status} />
                {report.unacknowledged_workers.includes(finding.worker) && (
                  <span className="chip chip-warning">Not in the synthesis</span>
                )}
              </summary>
              <p className="finding-label">Brief from the orchestrator</p>
              <p className="preserve-lines">{finding.task}</p>
              {finding.output && (
                <>
                  <p className="finding-label">Output</p>
                  <p className="preserve-lines">{finding.output}</p>
                </>
              )}
              {finding.error && <p className="worker-error">{finding.error}</p>}
              {finding.trace.length > 0 && (
                <>
                  <p className="finding-label">Trace</p>
                  <TraceSteps steps={finding.trace} />
                </>
              )}
            </details>
          ))}
        </>
      )}

      <h3>Tokens and cost</h3>
      <div className="table-scroll">
        <table>
          <caption>{report.cost.basis}</caption>
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">Model</th>
              <th scope="col" className="num">
                Input
              </th>
              <th scope="col" className="num">
                Output
              </th>
              <th scope="col" className="num">
                Cost
              </th>
            </tr>
          </thead>
          <tbody>
            {report.cost.lines.map((line) => (
              <CostRow key={line.label} line={line} />
            ))}
          </tbody>
          <tfoot>
            <tr>
              <th scope="row" colSpan={4}>
                Total
              </th>
              <td className="num">{usd(report.cost.total_usd)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}
