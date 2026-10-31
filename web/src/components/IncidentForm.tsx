import { useState, type FormEvent } from "react";
import type { ClientConfig, Incident, RunRequest, Severity } from "../lib/types";

const SEVERITIES: Severity[] = ["sev1", "sev2", "sev3", "sev4"];

interface Props {
  config: ClientConfig;
  samples: Incident[];
  running: boolean;
  onSubmit: (request: RunRequest, accessToken?: string) => void;
  onStop: () => void;
}

export function IncidentForm({ config, samples, running, onSubmit, onStop }: Props) {
  const first = samples[0];
  const [sampleId, setSampleId] = useState(first?.id ?? "");
  const [title, setTitle] = useState(first?.title ?? "");
  const [severity, setSeverity] = useState<Severity>(first?.severity ?? "sev2");
  const [service, setService] = useState(first?.service ?? "");
  const [description, setDescription] = useState(first?.description ?? "");
  const [failure, setFailure] = useState("");
  const [accessToken, setAccessToken] = useState("");

  function chooseSample(id: string) {
    setSampleId(id);
    const sample = samples.find((s) => s.id === id);
    if (!sample) return;
    setTitle(sample.title);
    setSeverity(sample.severity);
    setService(sample.service ?? "");
    setDescription(sample.description);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    // Keep the sample's other fields (source, start time), which the form has no inputs for.
    const sample = samples.find((s) => s.id === sampleId);
    const incident: Incident = {
      ...(sample && { id: sample.id, source: sample.source, started_at: sample.started_at }),
      title,
      severity,
      description,
      service: service || null,
    };
    onSubmit(
      { incident, simulate_failures: failure ? [failure] : [] },
      accessToken ? accessToken : undefined,
    );
  }

  return (
    <form className="incident-form" onSubmit={submit}>
      <h2>Report an incident</h2>

      <label>
        Start from a sample
        <select value={sampleId} onChange={(e) => chooseSample(e.target.value)}>
          {samples.map((sample) => (
            <option key={sample.id} value={sample.id}>
              {sample.title}
            </option>
          ))}
        </select>
      </label>

      <label>
        Title
        <input required maxLength={200} value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>

      <div className="field-row">
        <label>
          Severity
          <select value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
            {SEVERITIES.map((value) => (
              <option key={value}>{value}</option>
            ))}
          </select>
        </label>
        <label>
          Service <span className="optional">(optional)</span>
          <input maxLength={64} value={service} onChange={(e) => setService(e.target.value)} />
        </label>
      </div>

      <label>
        Description
        <textarea
          required
          rows={6}
          maxLength={4000}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </label>

      {config.can_simulate_failures && (
        <label>
          Simulate a worker failure
          <select value={failure} onChange={(e) => setFailure(e.target.value)}>
            <option value="">None</option>
            {config.workers.map((worker) => (
              <option key={worker.name}>{worker.name}</option>
            ))}
          </select>
        </label>
      )}

      {config.auth_required && (
        <label>
          Access token
          <input
            type="password"
            autoComplete="off"
            required
            value={accessToken}
            onChange={(e) => setAccessToken(e.target.value)}
          />
        </label>
      )}

      {running ? (
        <button type="button" className="secondary" onClick={onStop}>
          Stop
        </button>
      ) : (
        <button type="submit">Respond to incident</button>
      )}
    </form>
  );
}
