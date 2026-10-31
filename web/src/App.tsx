import { useEffect, useState } from "react";
import { IncidentForm } from "./components/IncidentForm";
import { PlanPanel } from "./components/PlanPanel";
import { ReportView } from "./components/ReportView";
import { WorkerCard } from "./components/WorkerCard";
import { fetchConfig, fetchSamples } from "./lib/api";
import type { RunState } from "./lib/runState";
import type { ClientConfig, Incident } from "./lib/types";
import { useRun } from "./lib/useRun";

function statusText(state: RunState): string {
  const running = state.workers.filter((worker) => worker.status === "running").length;
  switch (state.phase) {
    case "done":
      return "Report ready.";
    case "failed":
      return "The run failed.";
    case "stopped":
      return "Stopped.";
    case "running":
      if (state.synthesising) return "Synthesising the report…";
      if (running > 0) return `${running} ${running === 1 ? "worker" : "workers"} running…`;
      return "Orchestrator deciding…";
    default:
      return "";
  }
}

function Introduction({ config }: { config: ClientConfig }) {
  return (
    <section aria-labelledby="intro-heading" className="intro">
      <h2 id="intro-heading">How this works</h2>
      <p>
        An orchestrator agent reads the incident and decides which specialists it needs. Nothing
        fixes the team or the order in advance, so try a few samples and watch the plan change.
        Specialists requested together run at the same time. The orchestrator then writes one report
        from their findings.
      </p>
      <h3>The specialists on call</h3>
      <dl className="worker-list">
        {config.workers.map((worker) => (
          <div key={worker.name}>
            <dt>
              <code>{worker.name}</code>
            </dt>
            <dd>{worker.description}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export default function App() {
  const [config, setConfig] = useState<ClientConfig | null>(null);
  const [samples, setSamples] = useState<Incident[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const { state, start, stop } = useRun();

  useEffect(() => {
    Promise.all([fetchConfig(), fetchSamples()])
      .then(([loadedConfig, loadedSamples]) => {
        setConfig(loadedConfig);
        setSamples(loadedSamples);
      })
      .catch(() => setLoadError("Could not reach the API. Is it running?"));
  }, []);

  const running = state.phase === "running";

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Incident orchestrator</h1>
          <p className="muted">Strands Agents · orchestrator-workers pattern</p>
        </div>
        {config && (
          <p className="models">
            {config.llm_mode === "fake" ? (
              <span className="mode-fake">Rule-based fake LLM: no AWS, no real model</span>
            ) : (
              <>
                Orchestrator <code>{config.orchestrator_model}</code>
                <br />
                Workers <code>{config.worker_model}</code>
              </>
            )}
          </p>
        )}
      </header>

      {loadError && (
        <p role="alert" className="banner banner-error">
          {loadError}
        </p>
      )}

      {config && (
        <main className="layout">
          <aside>
            <IncidentForm
              key={samples[0]?.id}
              config={config}
              samples={samples}
              running={running}
              onSubmit={(request, token) => void start(request, token)}
              onStop={stop}
            />
          </aside>

          <div className="run">
            <p role="status" className="run-status">
              {statusText(state)}
            </p>

            {state.phase === "idle" && <Introduction config={config} />}

            {state.phase === "failed" && (
              <p role="alert" className="banner banner-error">
                {state.error}
              </p>
            )}

            {state.phase !== "idle" && <PlanPanel state={state} />}

            {state.workers.length > 0 && (
              <section aria-labelledby="workers-heading">
                <h2 id="workers-heading">Workers</h2>
                <div className="worker-grid">
                  {state.workers.map((worker) => (
                    <WorkerCard key={worker.toolUseId} worker={worker} />
                  ))}
                </div>
              </section>
            )}

            {state.report && <ReportView report={state.report} />}
          </div>
        </main>
      )}
    </div>
  );
}
