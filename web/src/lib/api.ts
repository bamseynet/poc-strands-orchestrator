import { parseSse } from "./sse";
import type { ClientConfig, Incident, RunEvent, RunRequest } from "./types";

export class ApiError extends Error {}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw new ApiError(`${path} returned ${response.status}.`);
  return (await response.json()) as T;
}

export const fetchConfig = () => getJson<ClientConfig>("/api/config");
export const fetchSamples = () => getJson<Incident[]>("/api/samples");

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function explain(response: Response): Promise<string> {
  if (response.status === 401) return "That access token was not accepted.";
  if (response.status === 422) return "The server rejected this: check the incident's fields.";
  if (response.status === 429) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    return body.detail ?? "Too many runs are in progress. Try again shortly.";
  }
  return `The server returned ${response.status}.`;
}

export interface RunOptions {
  accessToken?: string;
  signal?: AbortSignal;
}

/** Starts a run and returns its events. Rejects with ApiError if the server refuses to start it. */
export async function startRun(
  request: RunRequest,
  { accessToken, signal }: RunOptions = {},
): Promise<AsyncGenerator<RunEvent>> {
  const body = JSON.stringify(request);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    // CloudFront can only sign a POST to a Lambda function URL if the client sends the body's hash.
    "x-amz-content-sha256": await sha256Hex(body),
  };
  if (accessToken) headers["X-Access-Token"] = accessToken;

  const response = await fetch("/api/runs", { method: "POST", headers, body, signal });
  if (!response.ok) throw new ApiError(await explain(response));
  if (!response.body) throw new ApiError("The server sent no event stream.");
  return parseSse(response.body) as AsyncGenerator<RunEvent>;
}
