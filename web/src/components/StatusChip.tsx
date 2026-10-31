import type { WorkerRun } from "../lib/types";

const DISPLAY: Record<WorkerRun["status"], { icon: string; label: string }> = {
  running: { icon: "◌", label: "Running" },
  ok: { icon: "✓", label: "Done" },
  failed: { icon: "✕", label: "Failed" },
};

export function StatusChip({ status }: { status: WorkerRun["status"] }) {
  const { icon, label } = DISPLAY[status];
  return (
    <span className={`chip chip-${status}`}>
      <span aria-hidden="true">{icon}</span> {label}
    </span>
  );
}
