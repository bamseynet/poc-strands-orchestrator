import { seconds } from "../lib/format";
import type { WorkerRun } from "../lib/types";

function percent(value: number, total: number): string {
  return total > 0 ? `${(value / total) * 100}%` : "0%";
}

/** One bar per worker call against the whole run, so parallel work shows up as overlap. */
export function Timeline({ findings, totalMs }: { findings: WorkerRun[]; totalMs: number }) {
  return (
    <ul className="timeline">
      {findings.map((finding) => {
        const duration = finding.duration_ms ?? 0;
        return (
          <li key={finding.tool_use_id}>
            <span className="timeline-label">{finding.worker}</span>
            <span className="timeline-track">
              <span
                data-testid="bar"
                className={`timeline-bar timeline-bar-${finding.status}`}
                style={{
                  left: percent(finding.started_ms, totalMs),
                  width: percent(duration, totalMs),
                }}
              />
            </span>
            <span className="visually-hidden">
              started at {seconds(finding.started_ms)}, took {seconds(duration)}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
